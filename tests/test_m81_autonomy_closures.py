# -*- coding: utf-8 -*-
"""M81：完全自主三缺口闭合回归。

生产实证（2026-11-24 夜间）：model_retry_exhausted 停在 needs_reconciliation
死等主人 5 小时零进展；交付阻塞同样只能主人 retry；验收自指让"写 .swift
不编译"也能过（D22）。三项修复：
① RunCoordinator.auto_recover——模型重试耗尽自动续接（长冷却+有界）；
② 交付阻塞自动恢复（冷却后自动 retry+换打法反馈，上限耗尽才留给人）；
③ 工位存在 .swift 源码即强制真实编译回执（工程事实优先于任务措辞）。
"""
import json
import shutil
import time

import pytest

from proto.exec.agentic import LOOP_SYSTEM, AgenticLoop, _has_swift_sources
from proto.fleet.manager import AgentManager
from proto.runtime.agent import ProtoAgent
from proto.work.delivery import (DELIVERY_AUTO_RETRY_COOLDOWN_S, DELIVERY_AUTO_RETRY_FIRST_S,
                                 DELIVERY_AUTO_RETRY_MAX)
from proto.work.journal import GoalJournal


class Scripted:
    def __init__(self, decisions=(), verdicts=()):
        self.decisions = list(decisions)
        self.verdicts = list(verdicts)
        self.actor_inputs = []
        self.verifier_inputs = []

    def complete(self, system, user):
        if system == LOOP_SYSTEM:
            self.actor_inputs.append((system, user))
            reply = self.decisions.pop(0)
        elif system.startswith("你是独立验收员"):
            self.verifier_inputs.append((system, user))
            reply = self.verdicts.pop(0)
        else:
            return json.dumps({"acceptance": ""}, ensure_ascii=False)
        return json.dumps(reply, ensure_ascii=False)


# ---- ① 模型重试耗尽自动续接 ----

class TestAutoRecoverRuns:
    @pytest.fixture()
    def fleet(self, tmp_path):
        manager = AgentManager(tmp_path, llm=Scripted())
        identifier = manager.create("工程师")["id"]
        agent = manager.get(identifier)
        agent.configure_autonomy(enabled=True, mode="auto")
        return manager

    @staticmethod
    def _exhausted(fleet, *, age=3600, auto=0):
        # 真跑一条短链生成可安全续作的会话，再伪装成"模型重试耗尽"
        agent = fleet.get(fleet.current_id())
        agent.llm.decisions = [{"tool": "clock", "args": {}, "category": "read_draft"},
                               {"done": True, "summary": "ok"}]
        goal = agent.goal_tree.add("临时目标", "mid", acceptance="x")
        done = AgenticLoop(agent, max_steps=3, pool="self_initiated").run(
            "临时目标", acceptance="x", goal_id=goal.id,
            journal=GoalJournal(agent.data_dir / "work" / goal.id / "JOURNAL.md"))
        # 会话翻回 waiting_model——auto_recover 只续作等模型的会话
        session = agent.sessions.lookup(done["session_id"])
        session["status"] = "waiting_model"
        session["pending_call"] = None
        agent.sessions.store.put("execution_sessions", done["session_id"], session)
        run = fleet.runs.enqueue(agent.fleet_id, "tick", {})
        with fleet.runs.database.connect(write=True) as connection:
            connection.execute(
                "UPDATE runs SET status='needs_reconciliation', reason='model_retry_exhausted', "
                "result=?, session_id=?, updated_at=? WHERE id=?",
                (json.dumps({"resume_session": True, "status": "waiting_model",
                             "auto_recovers": auto, "session_id": done["session_id"]}),
                 done["session_id"], time.time() - age, run["id"]))
        return run

    def test_recovers_stale_exhausted_run(self, fleet):
        run = self._exhausted(fleet)
        recovered = fleet.runs.auto_recover()
        assert run["id"] in recovered
        after = fleet.runs.get(run["id"])
        assert after["status"] == "queued" and after["reason"] == "safe_resume_ready"
        assert after["result"]["auto_recovers"] == 1

    def test_fresh_exhausted_run_waits_for_cooldown(self, fleet):
        run = self._exhausted(fleet, age=10)
        assert fleet.runs.auto_recover() == []
        assert fleet.runs.get(run["id"])["status"] == "needs_reconciliation"

    def test_cap_exhausted_not_recovered(self, fleet):
        from proto.fleet.runs import AUTO_RECOVER_MAX
        run = self._exhausted(fleet, auto=AUTO_RECOVER_MAX)
        assert fleet.runs.auto_recover() == []

    def test_cancelled_run_not_recovered(self, fleet):
        run = self._exhausted(fleet)
        with fleet.runs.database.connect(write=True) as connection:
            connection.execute("UPDATE runs SET cancel_requested=1 WHERE id=?", (run["id"],))
        assert fleet.runs.auto_recover() == []

    def test_unknown_effect_reason_untouched(self, fleet):
        agent = fleet.get(fleet.current_id())
        run = fleet.runs.enqueue(agent.fleet_id, "tick", {})
        with fleet.runs.database.connect(write=True) as connection:
            connection.execute(
                "UPDATE runs SET status='needs_reconciliation', reason='safe_resume_limit', "
                "updated_at=? WHERE id=?", (time.time() - 3600, run["id"]))
        assert fleet.runs.auto_recover() == []


# ---- ② 交付阻塞自动恢复 ----

class TestDeliveryAutoRetry:
    def _blocked_goal(self, tmp_path, *, age=DELIVERY_AUTO_RETRY_FIRST_S + 60):
        agent = ProtoAgent(tmp_path, llm=Scripted())
        goal = agent.goal_tree.add("实现小工具", "mid", acceptance="需要真实结果")
        agent.spine.store.put("goal_delivery", goal.id, {
            "slices": 2, "stalled": 2, "blocked": True, "reason": "delivery_no_progress",
            "blocked_at": time.time() - age, "verified": False})
        return agent, goal

    def test_stale_block_auto_recovers_with_feedback(self, tmp_path):
        agent, goal = self._blocked_goal(tmp_path)
        agent._auto_recover_delivery(agent.goal_tree.all_active())
        state = agent.delivery_state(goal.id)
        assert not state["blocked"] and state["auto_retries"] == 1 and state["auto_feedback"] is True
        # 下一轮 work_on_goal 必须收到换打法指令（反馈注入在任务文本里）
        agent.llm.decisions = [{"tool": "clock", "args": {}, "category": "read_draft"},
                               {"done": True, "summary": "ok"}]
        agent.llm.verdicts = [{"verdict": "fail", "missing": ["x"]}]
        agent.work_on_goal(goal)
        assert any("禁止重复此前失败的路径" in system + user
                   for system, user in agent.llm.actor_inputs)
        assert agent.delivery_state(goal.id).get("auto_feedback") is not True

    def test_fresh_block_waits_cooldown(self, tmp_path):
        agent, goal = self._blocked_goal(tmp_path, age=10)
        agent._auto_recover_delivery(agent.goal_tree.all_active())
        assert agent.delivery_state(goal.id)["blocked"] is True

    def test_cap_exhausted_stays_for_owner(self, tmp_path):
        agent, goal = self._blocked_goal(tmp_path)
        agent.spine.store.put("goal_delivery", goal.id, {
            "slices": 2, "stalled": 2, "blocked": True, "reason": "delivery_no_progress",
            "blocked_at": time.time() - 99999, "auto_retries": DELIVERY_AUTO_RETRY_MAX})
        agent._auto_recover_delivery(agent.goal_tree.all_active())
        assert agent.delivery_state(goal.id)["blocked"] is True

    def test_available_work_recovers_then_selects(self, tmp_path):
        agent, goal = self._blocked_goal(tmp_path)
        agent._available_work()
        assert not agent.delivery_state(goal.id)["blocked"]


# ---- ③ Swift 源码强制编译回执 ----

class TestSwiftCompileRequirement:
    def test_has_swift_sources_detection(self, tmp_path):
        (tmp_path / "projects" / "app").mkdir(parents=True)
        (tmp_path / "projects" / "app" / "Main.swift").write_text("let x = 1", encoding="utf-8")

        class W:
            root = tmp_path
        assert _has_swift_sources(W(), "projects/app", []) is True
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "README.md").write_text("x", encoding="utf-8")
        assert _has_swift_sources(W(), "docs", []) is False
        assert _has_swift_sources(W(), "projects",
                                   [{"args": {"path": "projects/app/A.swift"}}]) is True

    def test_swift_done_without_compile_not_verified(self, tmp_path):
        agent = ProtoAgent(tmp_path, llm=Scripted())
        goal = agent.goal_tree.add("以 SwiftUI 实现小界面", "mid", acceptance="自写脚本全绿即可")
        project = tmp_path / "workspace" / "projects" / "ui"
        project.mkdir(parents=True, exist_ok=True)
        (project / "Main.swift").write_text("import SwiftUI\nstruct V: View { var body = some View { Text(\"hi\") } }",
                                            encoding="utf-8")
        agent.llm.decisions = [{"tool": "write_file", "args": {"path": "projects/ui/View.swift",
                                                                "content": "struct Row {}"},
                                "category": "workspace_write"},
                               {"done": True, "summary": "写完了"}]
        agent.llm.verdicts = [{"verdict": "pass"}]  # 即使语义门放行……
        result = AgenticLoop(agent, max_steps=4, pool="owner_tasks").run(
            "以 SwiftUI 实现小界面", acceptance="自写脚本全绿即可", goal_id=goal.id,
            journal=GoalJournal(tmp_path / "work" / goal.id / "JOURNAL.md"),
            project_dir="projects/ui")
        # 确定性门先于语义门：没有真实编译回执不得 verified
        assert result.get("verified") is not True
        assert (result["status"] in ("awaiting_retry", "max_steps", "failed")
                or result.get("missing"))
        # 门在轨迹里留下"必须编译"的纠偏观察
        assert any("本地构建" in o or "回执" in o for o in result.get("observations", []))

    @pytest.mark.skipif(not shutil.which("xcrun"), reason="需要本机 swiftc")
    def test_swift_with_real_typecheck_passes_deterministic_gate(self, tmp_path):
        agent = ProtoAgent(tmp_path, llm=Scripted())
        goal = agent.goal_tree.add("实现纯逻辑 Swift 模块", "mid", acceptance="typecheck exit 0")
        project = tmp_path / "workspace" / "projects" / "core"
        project.mkdir(parents=True, exist_ok=True)
        agent.llm.decisions = [
            {"tool": "write_file", "args": {"path": "projects/core/Core.swift",
                                            "content": "struct Core { static func add(_ a: Int, _ b: Int) -> Int { a + b } }"},
             "category": "workspace_write"},
            {"tool": "run_command",
             "args": {"argv": ["xcrun", "swiftc", "-typecheck", "projects/core/Core.swift"]},
             "category": "workspace_write"},
            {"done": True, "summary": "写完并 typecheck 通过"},
        ]
        agent.llm.verdicts = [{"verdict": "pass"}]
        result = AgenticLoop(agent, max_steps=6, pool="owner_tasks").run(
            "实现纯逻辑 Swift 模块", acceptance="typecheck exit 0", goal_id=goal.id,
            journal=GoalJournal(tmp_path / "work" / goal.id / "JOURNAL.md"),
            project_dir="projects/core")
        assert result.get("missing") is None or result["status"] != "awaiting_retry" \
            or "local_verification" not in str(result.get("reason", ""))
        assert result["verified"] is True
