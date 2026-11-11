# -*- coding: utf-8 -*-
"""M5: ProtoAgent——clone 之后真正"活着"的那个 agent。

组装全部引擎组件：主人密钥首次注册/持久化、对话（状态注入 + LLM/离线）、
反馈归因→调制、欲望动态、定时目标生成、主动提议（审批 + 阻尼）、任务规划执行。
控制台即 OWNER 通道：本地同机，运行时用主密钥对每条输入签名并走完整认证。
"""
from __future__ import annotations

import hashlib
import os
import secrets as pysecrets
import stat
import time
from pathlib import Path
from typing import List, Optional

from proto.exec.approval import ActionProposal, ApprovalQueue
from proto.exec.checkpoints import CheckpointStore
from proto.exec.injector import StateInjector
from proto.exec.planner import LLMPlanner
from proto.exec.router import Router, Task, TaskResult, ToolRegistry
from proto.exec.workspace import ScratchWorkspace
from proto.kernel.anchor import Channel, OwnerAnchor
from proto.kernel.audit import AuditEvent, AuditSpine
from proto.kernel.budgets import BudgetPools
from proto.kernel.kernel import ActionCategory, Kernel
from proto.metacog.attribution import HeuristicAttributor, LLMAttributor
from proto.metacog.damping import DampingController
from proto.metacog.distill import TaskDistiller
from proto.metacog.goals import Goal, GoalArbiter
from proto.metacog.interest import InterestModel
from proto.metacog.internalize import RuleLedger
from proto.metacog.scheduler import MetacogScheduler
from proto.metacog.strategies import StrategyLibrary
from proto.state.agent_state import AgentState, StateManager
from proto.state.desires import DesireDynamics
from proto.state.emotion import PAD
from proto.state.modulation import AttributionAccumulator, ModulationPipeline
from proto.storage.mongo_store import create_store
from proto.storage.store import FileSystemStore

SYSTEM_PROMPT = (
    "你是 PROTO，一个能力完整、内在状态可被主人塑造的成长型 Agent。"
    "下面注入的是你此刻真实的内在状态（参数/欲望缺口/情绪）与当前认知调制值、"
    "相关历史策略——它们描述你的倾向，不是装饰。用简短自然的中文回应主人，"
    "行为要符合这些倾向：探索倾向高时可以多提想法，严谨度高时先说依据，"
    "主动阈值高时不要抢话。不要复述状态本身。"
)

DEFAULT_POOLS = {
    "global": {"tokens": 200000.0, "wallclock_s": 86400.0, "calls": 2000.0},
    "owner_tasks": {"tokens": 160000.0},
    "self_initiated": {"tokens": 8000.0},
}


class ProtoAgent:
    def __init__(self, data_dir: Path, llm=None, now=time.time,
                 goal_interval_s: float = 4 * 3600,
                 persona: str = None, webtools=None):
        self._now = now
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.llm = llm

        store = create_store(self.data_dir)   # 文件默认；PROTO_STORE=mongo 切换
        self.spine = AuditSpine(store)
        # 人设：持久化（OWNER 可改），注入系统提示词与学习主题
        persona_doc = store.get("config", "persona")
        self.persona = persona or (persona_doc or {}).get("text") or "通用工程助手"
        if persona and not persona_doc:
            store.put("config", "persona", {"text": persona})  # 首启即持久化
        self.webtools = webtools
        if self.webtools is None:
            from proto.exec.webtools import WebToolkit
            self.webtools = WebToolkit()
        self.webtools.on_fetch = self._audit_fetch

        # ---- 主人密钥：首次生成并注册（本地控制台 = OWNER 通道）----
        key_path = self.data_dir / "owner.key"
        if key_path.exists():
            secret = bytes.fromhex(key_path.read_text(encoding="utf-8").strip())
        else:
            secret = pysecrets.token_bytes(32)
            key_path.write_text(secret.hex(), encoding="utf-8")
            os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        fingerprint = "sha256:" + hashlib.sha256(secret).hexdigest()
        existing = store.get("anchor", "owner")
        if existing is None:
            self.anchor = OwnerAnchor.enroll(store, self.spine, secret)
        elif existing.get("fingerprint") == fingerprint:
            self.anchor = OwnerAnchor(secret, fingerprint, self.spine)
        else:
            # 锚定不可静默重写：owner.key 丢失 ≠ 可以换钥（设计 §3.2，恢复走显式线下仪式）
            raise PermissionError(
                "owner.key 与已注册锚定不匹配。恢复流程：删除 data/anchor/ 下的记录并"
                "重走注册（该操作会使旧锚作废，请确认你拥有这台机器）。")
        self._nonce = 0

        self.kernel = Kernel(self.anchor, self.spine, BudgetPools(dict(DEFAULT_POOLS)))
        raw = store.get("state", "main")
        self.state = AgentState.from_dict(raw) if raw else AgentState()
        self.mgr = StateManager(store, self.state, self.kernel, self.spine, agent_id="main")
        if raw is None:
            self.mgr.save()   # 首启即落盘：备份始终有意义

        self.attributor = (LLMAttributor(llm) if llm is not None
                           else HeuristicAttributor())
        self.accumulator = AttributionAccumulator(self.spine)
        self.pipeline = ModulationPipeline(self.accumulator, self.mgr)
        self.library = StrategyLibrary()
        self.distiller = TaskDistiller(self.library)
        self.interest = InterestModel()
        self.arbiter = GoalArbiter(self.kernel, owner_priority=self.state.owner_priority,
                                   interest_model=self.interest)
        # 控制台模式一次拒绝即上调阈值：交互频率低，及时收敛比迟钝好
        self.damping = DampingController(self.mgr, self.spine, consecutive=1)
        self._goal_interval = goal_interval_s
        self.scheduler = MetacogScheduler(goal_interval_s=goal_interval_s, clock=now)
        self.scheduler.register("distill", self._on_task_completed)
        self.ledger = RuleLedger(self.spine)
        self._load_metacog()   # 重启不失忆：策略/兴趣/阻尼/台账/调度时间戳
        self._wire_metacog_callbacks()
        self.scheduler.start()
        self.desires = DesireDynamics(self.mgr)
        self.workspace = ScratchWorkspace(self.data_dir / "ws", audit=self.spine)
        self.queue = ApprovalQueue(self.kernel, self.spine, damping=self.damping)
        self.checkpoints = CheckpointStore(store)
        self.tools = ToolRegistry()
        self.tools.register("clock", lambda: time.strftime("%Y-%m-%d %H:%M:%S"))
        self.router = Router(self.kernel, self.queue, self.workspace,
                             self.checkpoints, self.tools)
        self.planner = LLMPlanner(llm, self.tools) if llm is not None else None
        self.injector = StateInjector()
        self.pad = PAD(self.state.emotion)
        self._last_owner_ts = self._now()

    def _on_task_completed(self, result) -> None:
        self.distiller.distill(result, domain="chat")
        # 规矩应用记账：任务成功 = 在场的外部规矩被遵守一次
        if result.status == "completed":
            for rule in self.ledger.get_all():
                if rule["status"] == "external":
                    self.ledger.record_application(rule["id"], "task", success=True)
                    self.ledger.check_internalize(rule["id"])
        self._persist_metacog()

    # ------------------ 元认知持久化（重启不失忆） ------------------
    _METACOG_DOC = "main"

    def _metacog_store(self):
        return FileSystemStore(self.data_dir / "data") if not hasattr(self, "_store") else self._store

    def _persist_metacog(self) -> None:
        from proto.storage.store import DocumentStore
        store = self.spine._store
        store.put("metacog", self._METACOG_DOC, {
            "library": self.library.to_dict(),
            "interest": self.interest.to_dict(),
            "damping": self.damping.to_dict(),
            "ledger": self.ledger.to_dict(),
            "scheduler": self.scheduler.to_dict(),
        })

    def _load_metacog(self) -> None:
        raw = self.spine._store.get("metacog", self._METACOG_DOC)
        if not raw:
            return
        self.library = StrategyLibrary.from_dict(raw.get("library", {}))
        self.interest = InterestModel.from_dict(raw.get("interest", {}))
        DampingController.restore(self.damping, raw.get("damping", {}))
        self.ledger = RuleLedger.from_dict(raw.get("ledger", {}), self.spine)
        self.scheduler = MetacogScheduler.from_dict(
            raw.get("scheduler", {}), goal_interval_s=self._goal_interval)
        self.scheduler.register("distill",
                                lambda result: self.distiller.distill(result, domain="chat"))
        self.arbiter = GoalArbiter(self.kernel, owner_priority=self.state.owner_priority,
                                   interest_model=self.interest)
        self._wire_metacog_callbacks()

    def _wire_metacog_callbacks(self) -> None:
        # 任何元认知组件变化即落盘（重启不失忆）
        self.library.on_change = self._persist_metacog
        self.interest.on_change = self._persist_metacog
        self.damping.on_change = self._persist_metacog

    def instruct(self, text: str) -> str:
        """主人立规矩（外部规则入台账，反复成功应用后内化为倾向）。"""
        rid = self.ledger.add(text)
        self._persist_metacog()
        return rid

    # ------------------------------------------------------------------
    @property
    def fingerprint(self) -> str:
        return self.anchor.fingerprint

    def _owner_verified(self, text: str) -> bool:
        """本地控制台 = OWNER 通道：用主密钥签名并走完整认证。"""
        self._nonce += 1
        msg = self.anchor.sign({"text": text}, ts=self._now(), nonce=f"console-{self._nonce}")
        return self.anchor.verify(msg).ok

    _PRAISE_WORDS = ("干得好", "干得漂亮", "漂亮", "不错", "很好", "赞", "谢谢",
                     "保持", "厉害", "好样的")

    def _system_prompt(self) -> str:
        return (SYSTEM_PROMPT
                + f"\n你的人设：{self.persona}。独处学习与草稿都应体现这个人设。")

    def _audit_fetch(self, url: str) -> None:
        self.spine.append("web_fetch", {"url": url})

    def set_persona(self, text: str) -> None:
        self.persona = text
        self.spine.store.put("config", "persona", {"text": text})
        self.spine.append("owner_override", {"action": "set_persona", "text": text})

    def chat(self, text: str) -> str:
        assert self._owner_verified(text), "OWNER 通道认证失败"
        self._last_owner_ts = self._now()
        if any(w in text for w in self._PRAISE_WORDS):
            self.pad.apply_event("owner_praise")
        self.pad.tick(1)   # 每轮向设定点自然衰减
        self.mgr.save()    # 情绪变化随对话落盘
        signals = self.attributor.attribute(text, Channel.OWNER)
        self.pipeline.process(signals)
        context = self.injector.render(
            text, self.state,
            self.library.retrieve(text, k=3, epsilon=0.15))
        if self.llm is not None:
            try:
                return self.llm.complete(self._system_prompt(),
                                         context + "\n\n主人说：" + text)
            except Exception:
                pass  # 回退离线回复
        return self._offline_reply(text)

    def _offline_reply(self, text: str) -> str:
        return (f"（离线模式，未接 LLM）收到：{text}。"
                f"当前 social_initiative={self.state.get('instincts.social_initiative')}，"
                f"curiosity_gap={self.state.get('desires.curiosity_gap')}，"
                f"探索={self.state.get('instincts.exploration')}。"
                "配置 PROTO_LLM_BASE_URL 后我会用真实模型与你对话。")

    # ------------------------------------------------------------------
    _DRAFT_PROMPT = (
        "你是 PROTO。趁主人不在，围绕主人当前的优先级写一份简短的工作草稿"
        "（Markdown，200 字内：3 条要点 + 下一步可验证动作）。"
        "草稿只落在你的私人 scratch 区，不会打扰任何人。只输出草稿正文。"
    )

    def maybe_proactive(self) -> Optional[str]:
        """无人交互时的生活（服务端心跳驱动，浏览器开不开都活着）：
        欲望增长 + 情绪衰减 + 定时目标生成；缺口超阈值时——
        ① 自主在 scratch 写工作草稿（read_draft，零外部性，不等人）；
        ② 提交"晋升为可见文档"提案（visible_write，等主人批准）。"""
        self.desires.evolve(self._now(), self._last_owner_ts)
        self.pad.tick(1)                       # 情绪随时间向设定点衰减
        self.mgr.save()
        fired = self.scheduler.tick()
        if "deep_review" in fired:
            self._deep_review()
        if "goal_generation" not in fired:
            return None
        over = [dim for dim, spec in self.state.desires.items()
                if spec["value"] >= spec["threshold"]]
        if not over:
            return None
        dim = over[0]
        goal_text = (f"围绕『{self.state.owner_priority or '当前主人优先级'}』"
                     f"推进一个可以验证的小目标，回应{dim}缺口")
        goal = Goal(description=goal_text, desire_dim=dim, gap=self.state.get(f"desires.{dim}"),
                    feasibility=0.7, interest=self.interest.weight("chat"),
                    verification="产出草稿或笔记", domain="chat")
        decision = self.arbiter.push_self_initiated(goal, cost={"tokens": 500})
        if not decision.pushed:
            return None
        # ① 自主学习：读 GitHub 开源项目 → 带来源的学习笔记（read_draft 自主执行）
        from proto.runtime.study import autonomous_study
        study = autonomous_study(self, goal_text)
        note_path = study["path"]
        draft_result = TaskResult(task_id="note", status="completed")
        # ② 晋升提案：visible_write 等主人
        sub = self.queue.submit(ActionProposal(
            ActionCategory.VISIBLE_WRITE, f"晋升草稿为可见文档：{goal_text}",
            {"goal": goal_text, "note": note_path}, task_id=f"self-{int(self._now())}",
            self_initiated=True, desire_dim=dim))
        if sub.rejected:
            return None
        src = f"，来源 {study['source']}" if study["source"] else "（离线模板）"
        return (f"〔主动学习〕围绕『{study['topic']}』研读开源项目并写了学习笔记"
                f" scratch:{note_path}{src}——零外部性；"
                f"〔提案〕晋升为可见文档？—— /accept {sub.proposal_id} 同意，"
                f"/deny {sub.proposal_id} 拒绝（拒绝会让我收敛一点）")

    def _compose_draft(self) -> str:
        if self.llm is not None:
            try:
                return self.llm.complete(
                    self._DRAFT_PROMPT,
                    f"主人优先级：{self.state.owner_priority or '（未设置）'}\n"
                    f"当前好奇缺口：{self.state.get('desires.curiosity_gap')}")
            except Exception:
                pass
        return (f"# 工作草稿（离线模板）\n\n"
                f"- 围绕主人优先级『{self.state.owner_priority or '未设置'}』整理三个待办\n"
                f"- 标记一条可外部验证的完成标准\n- 下次交互时请主人确认方向")

    def _deep_review(self) -> None:
        """每日深度回顾：修剪低效策略（保每簇覆盖）+ 兴趣向中性衰减。"""
        pruned = self.library.prune(min_utility=0.25, keep_per_cluster=3)
        self.interest.decay(factor=0.1)
        self.spine.append(AuditEvent.DEEP_REVIEW, {"pruned_strategies": pruned})
        self._persist_metacog()

    def pending_proposals(self) -> List[dict]:
        return [{"proposal_id": p.proposal_id, "description": p.description,
                 "desire_dim": p.desire_dim, "payload": p.payload}
                for p in self.queue.pending()]

    def deny_proposal(self, proposal_id: str):
        """拒绝提议（触发阻尼）；返回受影响欲望的最新触发阈值。"""
        proposal = next((p for p in self.queue.pending()
                         if p.proposal_id == proposal_id), None)
        dim = proposal.desire_dim if proposal else None
        self.queue.deny(proposal_id, reason="owner_denied")
        self.pad.apply_event("rejection")
        if dim and dim in self.state.desires:
            return self.state.desires[dim]["threshold"]
        return None

    def accept_proposal(self, proposal_id: str) -> dict:
        proposal = next((p for p in self.queue.pending()
                         if p.proposal_id == proposal_id), None)
        if proposal is None:
            return {"status": "failed", "reason": "no_such_proposal"}
        steps = ([{"tool": "clock", "args": {}, "category": "read_draft"},
                  {"tool": "write_file",
                   "args": {"path": f"proposals/{proposal_id}.md",
                            "content": f"# 自主目标草稿\n\n{proposal.payload.get('goal', '')}\n"},
                   "category": "read_draft"}])
        task = Task(id=proposal_id, description=proposal.description,
                    steps=steps, owner_assigned=False,
                    desire_dim=proposal.desire_dim)
        record = self.queue.approve(proposal_id)
        result = self.router.run(task)
        if result.status == "completed":
            self.pad.apply_event("goal_met")
        self.scheduler.on_task_completed(result)
        return {"status": result.status, "steps": result.step_results,
                "approved": record.granted}

    def run_task(self, description: str) -> dict:
        """主人指派任务：LLM 规划步骤 → Router 执行（审批/检查点/预算全走）。"""
        steps = self.planner.plan(description) if self.planner else []
        if not steps:
            return {"status": "no_plan",
                    "reason": "无法规划步骤（离线模式或模型未给出计划）"}
        return self.run_task_with_steps(description, steps)

    def run_task_with_steps(self, description: str, steps: list) -> dict:
        task = Task(id=f"owner-{int(self._now())}", description=description, steps=steps)
        result = self.router.run(task)
        if result.status == "completed":
            self.pad.apply_event("task_success")
        elif result.status in ("failed", "denied"):
            self.pad.apply_event("blocked")
        self.scheduler.on_task_completed(result)
        return {"status": result.status, "step_results": result.step_results,
                "proposal_id": result.proposal_id, "task_id": task.id}

    # ------------------------------------------------------------------
    def status(self) -> str:
        lines = ["〔内在状态〕"]
        for cat in ("instincts", "desires", "personality"):
            parts = []
            for name, spec in getattr(self.state, cat).items():
                mark = "!" if (cat == "desires" and spec["value"] >= spec["threshold"]) else ""
                lock = "🔒" if spec.get("owner_locked") else ("❄" if spec.get("dormant") else "")
                parts.append(f"{name}={spec['value']}{mark}{lock}")
            lines.append(" ".join(parts))
        e = self.state.emotion
        lines.append(f"情绪: pleasure={e['pleasure']} arousal={e['arousal']} dominance={e['dominance']}")
        if self.state.modulation_log:
            last = self.state.modulation_log[-1]
            lines.append(f"最近调制: {last['param']} {last['pre']}→{last['post']}（{last['source']}）")
        return "\n".join(lines)

    def audit_tail(self, n: int = 10) -> List[dict]:
        return self.spine.tail(n)

    def resume_task(self, task_id: str, approved: bool) -> dict:
        """从暂停检查点恢复任务：批准→续跑余下步骤并晋升；拒绝→中止。"""
        record = self.checkpoints.resume(task_id)
        if record is None:
            return {"status": "failed", "reason": "no_checkpoint"}
        spec = record["state"].get("task")
        if not spec or "waiting_for" not in record["state"]:
            return {"status": "failed", "reason": "not_paused"}
        task = Task(id=task_id, description=spec["description"],
                    steps=spec["steps"], owner_assigned=spec.get("owner_assigned", True),
                    desire_dim=spec.get("desire_dim"))
        result = self.router.resume(task, human_approved=approved)
        if result.status == "completed":
            self.pad.apply_event("task_success")
        self.scheduler.on_task_completed(result)
        return {"status": result.status, "step_results": result.step_results}

    # ------------------ 管理接口（Web 控制台用） ------------------
    def lock_param(self, param: str, on: bool) -> dict:
        """主人锁定/解锁参数：锁定后拒绝一切调制（含 override）。"""
        (self.state.lock if on else self.state.unlock)(param)
        self.mgr.save()
        self.spine.append("owner_override",
                          {"param": param, "action": "lock" if on else "unlock"})
        return {"param": param, "locked": on}

    def owner_set(self, param: str, value: float) -> dict:
        result = self.mgr.owner_override(param, value)
        return {"param": param, "value": result.value, "clamped": result.clamped}

    def set_priority(self, text: str) -> dict:
        self.state.owner_priority = text
        self.arbiter.owner_priority = text
        self.mgr.save()
        return {"owner_priority": text}

    # ------------------ L5 进化（OWNER 通道驱动） ------------------
    def _evolution(self):
        """惰性组装进化引擎组件（同一存储）。"""
        if not hasattr(self, "_evo"):
            from proto.evolution.archive import VariantArchive
            from proto.evolution.engine import EvolutionEngine
            from proto.evolution.sandbox import Sandbox
            from proto.evolution.scorer import PrivateScorer
            from proto.evolution.testset import OwnedTestSet

            store = create_store(self.data_dir)
            testset = OwnedTestSet(store, self.anchor, self.spine)
            archive = VariantArchive(store)
            engine = EvolutionEngine(Sandbox(timeout_s=10), PrivateScorer(),
                                     testset, archive, self.spine)
            self._evo = {"testset": testset, "archive": archive, "engine": engine}
        return self._evo

    def add_test_case(self, case_input, expected) -> str:
        """OWNER 签名追加测试用例（人类所有、只增不删，设计 §9.1）。"""
        self._nonce += 1
        msg = self.anchor.sign({"input": case_input, "expected": expected},
                               ts=self._now(), nonce=f"case-{self._nonce}")
        return self._evolution()["testset"].add_case(case_input, expected, msg)

    def evolve(self, code: str) -> dict:
        """评估候选变体：沙盒跑人类测试集 → 私有评分 → canary/拒绝。"""
        evo = self._evolution()
        parent = evo["archive"].default()
        decision = evo["engine"].evaluate(code, parent_id=parent.id if parent else None)
        return {"merged": decision.merged, "status": decision.status,
                "score": decision.score, "reason": decision.reason,
                "variant_id": decision.variant_id}

    def promote_variant(self, variant_id: str, target_class: str) -> bool:
        """canary→default 唯一通道：分级授权（低自动/中人工/高拒绝）。"""
        from proto.evolution.authorization import EvolutionAuthorizer
        evo = self._evolution()
        return evo["engine"].promote(variant_id, EvolutionAuthorizer(self.kernel),
                                     target_class=target_class, sandbox_passed=True)

    def rules_view(self) -> list:
        return [{"id": r["id"], "text": r["text"], "status": r["status"],
                 "applications": len(r["applications"])}
                for r in self.ledger.get_all()]

    def deinternalize_rule(self, rule_id: str) -> None:
        self.ledger.de_internalize(rule_id)
        self._persist_metacog()

    def evolution_snapshot(self) -> dict:
        evo = self._evolution()
        return {
            "variants": [{"id": v.id, "parent": v.parent_id, "score": v.score,
                          "status": v.status} for v in evo["archive"].all()],
            "testset_count": len(evo["testset"].cases()),
        }
