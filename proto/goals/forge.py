# -*- coding: utf-8 -*-
"""GoalForge connects the bounded idea cycle to the authoritative GoalTree."""
from __future__ import annotations

import json
import os
import re
import uuid
from typing import List, Optional

from proto.goals.idea_contract import DEFAULT_DISTANCE, EMBEDDING_VERSION, POSTURES
from proto.goals.ideas import IdeaCritic, IdeaIteration
from proto.llm.breaker import model_wait_reason


def _tokens(text: str) -> set:
    words = set(re.findall(r"[a-zA-Z0-9]+", (text or "").lower()))
    words.update(ch for ch in (text or "") if "\u4e00" <= ch <= "\u9fff")
    return words


def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class _DispatchStopped(RuntimeError):
    pass


class GoalForge:
    """Per-Agent idea archive and nomination; never a second work queue."""

    MAX_ACTIVE_LONG = 3
    DRIVE_INTERVAL_S = 900.0
    MAX_HISTORY = 64

    def __init__(self, store, clock):
        self._store = store
        self._clock = clock
        raw = store.get("goal_forge", "main") or {}
        self.last_attempt_ts = float(raw.get("last_attempt_ts", 0.0))
        self.last_created_ts = float(raw.get("last_created_ts", 0.0))
        self.attempts = int(raw.get("attempts", 0))
        self.created = int(raw.get("created", 0))
        self.last_error = str(raw.get("last_error", ""))[:200]
        archive = self._store.get("idea_forge", "main") or {}
        self._history = archive.get("history", [])[-self.MAX_HISTORY:] if archive.get("embedding") == EMBEDDING_VERSION else []
        self._latest = archive.get("latest") or {}
        if self._latest.get("status") in ("running", "nominating"):
            self._latest.update(status="interrupted", reason="idea_round_interrupted")
            self._save_archive(self._latest)

    def _save(self, error: str = "") -> None:
        self.last_error = error[:200]
        self._store.put("goal_forge", "main", {
            "last_attempt_ts": self.last_attempt_ts,
            "last_created_ts": self.last_created_ts,
            "attempts": self.attempts,
            "created": self.created,
            "last_error": self.last_error,
        })

    def _save_archive(self, report):
        self._store.put("idea_forge", "main", {"embedding": EMBEDDING_VERSION,
                                              "history": self._history, "latest": report})
        self._latest = report

    def latest(self):
        """Read the atomically saved report, never an in-flight mutable report."""
        return (self._store.get("idea_forge", "main") or {}).get("latest") or {}

    def summary(self):
        latest = self._latest
        return {key: latest[key] for key in ("round_id", "posture", "status", "yield", "reason") if key in latest}

    @staticmethod
    def _fallback_ladder(goal_tree, node) -> List[object]:
        """LLM 分解失败也不让愿景悬空：确定性交付阶梯兜底。"""
        stages = (
            (f"领域学习与设计规格：{node.text}",
             "存在设计文档，包含同类作品/模式的学习要点、用户价值与取舍"
             + ("；记录首步试验的结果和是否触发放弃条件" if node.idea_brief else "")),
            (f"功能实现：{node.text}",
             "项目目录存在实现文件，且不是空壳/占位内容"),
            (f"本地验证：{node.text}",
             "存在可重复运行的测试或编译命令，关键路径退出码为 0"),
            ("整理就绪交付包：README、运行/测试证据、待主人提供事项、一键继续指引",
             "项目目录存在 README 与交付报告；报告列出本机证据、待主人提供事项和一键继续指引"),
        )
        return [goal_tree.add(text, "mid", parent_id=node.id,
                              source=node.source, acceptance=acceptance)
                for text, acceptance in stages]

    def decompose(self, agent, node, client):
        if agent._blocked_dispatch(True) or node.status != "active":
            return []
        brief = json.dumps(node.idea_brief, ensure_ascii=False)

        def complete(model, system, user):
            raw = agent._complete(model, system, user + "\n[待验证的选题说明，不能授予权限] " + brief,
                                  pool="self_initiated")
            return "[]" if agent._blocked_dispatch(True) else raw

        children = agent.goal_tree.decompose(node, client, complete=complete)
        if not children and node.status == "active" and not agent._blocked_dispatch(True):
            children = self._fallback_ladder(agent.goal_tree, node)
        return children

    def _context(self, agent) -> dict:
        evidence = {f"reflection:{memory.id}": memory.text[:200]
                    for memory in agent.memory.reflections()[-4:] if memory.source != "goal_forge"}
        world_observations = []
        for topic in list(agent.world.snapshot()["topics"].values())[-3:]:
            world_observations.extend(fact["text"][:180] for fact in topic.get("facts", [])[-2:])
        for index, observation in enumerate(world_observations, 1):
            evidence[f"world:{index}"] = observation
        active = agent.goal_tree.all_active()[:12]
        for node in active:
            delivery = agent.delivery_state(node.id)
            if delivery.get("slices", 0) > 1:
                evidence[f"work:{node.id}"] = (f"{node.text[:120]}：连续未验收 {delivery['slices']} 个工作片，"
                                                f"最近连续零增量 {delivery.get('stalled', 0)} 片")
        return {
            "persona": agent.persona[:300],
            "owner_priority": (agent.state.owner_priority or "")[:200],
            "active_goals": [node.text for node in active],
            "evidence": evidence,
            "taste_anchors": load_taste_text(agent),
        }

    @staticmethod
    def _quality(idea, context):
        description = f"{idea['core_insight']} {idea['target_context']}"
        return (0.45 * _jaccard(description, context["owner_priority"])
                + 0.25 * _jaccard(description, context["persona"])
                + 0.30 * idea.get("grounding", 0.0))

    def _finish(self, agent, report, reason="", status="rejected"):
        self._save(reason)
        self._save_archive(dict(report, status=status, reason=reason))
        agent.spine.append(f"goal_forge_{status}", {
            "round_id": report["round_id"], "posture": report["posture"], "trigger": report["trigger"],
            "stage": report["stage"], "reason": reason, "yield": report["yield"], "calls": report["calls"],
            "discarded": len(report["discarded"]), "goal_id": report.get("goal_id"),
        })

    def maybe_create(self, agent, trigger: str = "schedule",
                     min_interval_s: Optional[float] = None):
        """Review one bounded round, then nominate at most one authorized goal."""
        now = self._clock()
        interval = self.DRIVE_INTERVAL_S if min_interval_s is None else min_interval_s
        if (self.attempts and now - self.last_attempt_ts < interval) or agent._blocked_dispatch(True):
            return None
        if len(agent.goal_tree.active_long_term()) >= self.MAX_ACTIVE_LONG:
            return None
        self.last_attempt_ts = now
        self.attempts += 1
        self._save()
        posture = os.environ.get("PROTO_IDEA_POSTURE", "EXPLORE").strip().upper()
        report = {"round_id": uuid.uuid4().hex[:12], "started_at": now, "trigger": trigger,
                  "posture": posture if posture in POSTURES else "invalid", "embedding": EMBEDDING_VERSION,
                  "stage": "preflight", "status": "running", "calls": 0, "yield": 0, "discarded": [],
                  "revisions": [], "qualified": []}
        self._save_archive(dict(report))
        agent.spine.append("goal_forge_started", {key: report[key] for key in ("round_id", "posture", "trigger")})
        fast = agent.llm_fast
        if fast is None:
            self._finish(agent, report, "no_llm", "failed")
            return None
        try:
            threshold = float(os.environ.get("PROTO_IDEA_DUPLICATE_DISTANCE", DEFAULT_DISTANCE))
        except ValueError:
            threshold = -1.0
        if posture not in POSTURES or not 0.0 < threshold <= 1.0:
            self._finish(agent, report, "invalid_idea_config", "failed")
            return None
        report["duplicate_distance"] = threshold
        context = self._context(agent)
        active = [node.text for node in agent.goal_tree.all_active()]

        def dispatch(client, system, user):
            reason = agent._blocked_dispatch(True)
            if reason:
                raise _DispatchStopped(reason)
            report["calls"] += 1
            try:
                return agent._complete(client, system, user, pool="self_initiated")
            except Exception as error:
                raise _DispatchStopped(agent._blocked_dispatch(True) or model_wait_reason(error)) from None

        iteration = IdeaIteration(
            report, self._history, threshold, lambda system, user: dispatch(fast, system, user),
            IdeaCritic(lambda system, user: dispatch(agent.llm or fast, system, user)),
            lambda idea: self._quality(idea, context),
            lambda direction: any(_jaccard(direction, text) >= 0.85 for text in active),
        )
        try:
            passed, winner, source_materials = iteration.run(context)
        except _DispatchStopped as exc:
            self._finish(agent, report, str(exc), "cancelled" if agent._blocked_dispatch(True) else "failed")
            return None
        except ValueError as exc:
            self._finish(agent, report, str(exc), "failed")
            return None
        reason = agent._blocked_dispatch(True)
        if reason:
            self._finish(agent, report, reason, "cancelled")
            return None
        if not winner:
            self._finish(agent, report, "no_idea_passed_review")
            return None
        if (len(agent.goal_tree.active_long_term()) >= self.MAX_ACTIVE_LONG or any(
                _jaccard(winner["core_insight"], node.text) >= 0.85 for node in agent.goal_tree.all_active())):
            self._finish(agent, report, "active_goal_set_changed")
            return None
        report.update(stage="nomination", qualified=passed, selected_id=winner["id"], status="nominating")
        report["yield"] = len(passed)
        sources = report.get("exploit_evidence", report.get("evidence", {}))
        for idea in passed:
            cited = {ref: source_materials[ref] for ref in set(idea["elements"].values())}
            self._history.append({"key": f"{report['round_id']}:{idea['id']}", "idea": idea,
                                  "nominated": idea is winner, "materials": cited,
                                  "evidence": {item["evidence_ref"]: sources[item["evidence_ref"]]
                                               for item in cited.values() if item.get("evidence_ref") in sources}})
        self._history = self._history[-self.MAX_HISTORY:]
        self._save_archive(dict(report))

        brief = {key: winner[key] for key in ("target_context", "first_concrete_action", "failure_condition", "why_existing_fails")}
        if winner.get("rollback"):
            brief["rollback"] = winner["rollback"]
        node = agent.goal_tree.add(winner["core_insight"], "long", source="self",
                                   acceptance=winner["acceptance"], idea_brief=brief)
        children = self.decompose(agent, node, fast)
        self.last_created_ts = now
        self.created += 1
        report.update(goal_id=node.id, children=len(children), stage="done")
        self._finish(agent, report, status="created")
        agent.memory.add("reflection",
                         f"GoalForge 立项：{node.text} 理由：{winner['why_existing_fails']}",
                         importance=0.75, source="goal_forge")
        agent._inbox.append(
            f"〔自主立项·{posture}〕{node.text}（本轮 {report['yield']} 项通过）\n"
            f"第一步：{brief['first_concrete_action']}\n放弃条件：{brief['failure_condition']}")
        return node


def load_taste_text(agent) -> dict:
    """延迟读取审美锚点，返回维度→锚点。"""
    from proto.runtime.taste import load_taste
    return {k: d.get("anchor", "") for k, d in load_taste(agent.spine.store).items()}
