# -*- coding: utf-8 -*-
"""M4-①: 参数调制回滚（含内化级联）+ 阻尼控制（设计 §11.2 / §7.5）。"""
import time
from datetime import datetime, timezone

import pytest

from proto.kernel.anchor import OwnerAnchor
from proto.kernel.audit import AuditSpine
from proto.kernel.budgets import BudgetPools
from proto.kernel.kernel import ActionCategory, Kernel
from proto.metacog.damping import DampingController
from proto.metacog.internalize import RuleLedger
from proto.state.agent_state import StateManager, AgentState
from proto.state.modulation import AttributionAccumulator, ModulationPipeline
from proto.state.store import FileSystemStore
from proto.exec.approval import ActionProposal, ApprovalQueue


@pytest.fixture()
def env(tmp_path):
    store = FileSystemStore(tmp_path / "data")
    spine = AuditSpine(store)
    anchor = OwnerAnchor.enroll(store, spine, b"\x01" * 32)
    pools = BudgetPools({"global": {"tokens": 10000.0}, "owner_tasks": {"tokens": 8000.0},
                         "self_initiated": {"tokens": 2000.0}})
    kernel = Kernel(anchor, spine, pools)
    state = AgentState()
    mgr = StateManager(store, state, kernel, spine)
    return store, spine, kernel, state, mgr


class TestModulationRollback:
    def test_rollback_restores_pre_window_values(self, env):
        _, spine, kernel, state, mgr = env
        window_start = time.time() - 1
        for _ in range(3):
            mgr.apply_modulation("instincts.exploration", +0.02, "owner")
            mgr.apply_modulation("personality.rigor", -0.02, "owner")
        assert state.get("instincts.exploration") == pytest.approx(0.66)
        result = mgr.rollback(since_ts=window_start)
        assert state.get("instincts.exploration") == pytest.approx(0.6)
        assert state.get("personality.rigor") == pytest.approx(0.7)
        assert {r["param"] for r in result["restored"]} == {"instincts.exploration", "personality.rigor"}
        assert spine.find(lambda d: d["event_type"] == "rollback")

    def test_rollback_respects_owner_locked(self, env):
        _, spine, kernel, state, mgr = env
        window_start = time.time() - 1
        mgr.apply_modulation("instincts.exploration", +0.02, "owner")
        state.lock("instincts.exploration")
        mgr.rollback(since_ts=window_start)
        assert state.get("instincts.exploration") == pytest.approx(0.62)  # 锁定不可回写

    def test_rollback_cascades_to_internalization(self, env):
        _, spine, kernel, state, mgr = env
        ledger = RuleLedger(spine)
        window_start = time.time() - 1
        mgr.apply_modulation("instincts.exploration", +0.02, "owner")
        rid = ledger.add("规则A")
        for _ in range(5):
            ledger.record_application(rid, "ctx", success=True)
        ledger.check_internalize(rid)
        assert ledger.get(rid)["status"] == "internal"
        mgr.rollback(since_ts=window_start, ledger=ledger)
        assert ledger.get(rid)["status"] == "external"   # 窗口内内化级联降级

    def test_rollback_ignores_pre_window_entries(self, env):
        import time as _t
        _, spine, kernel, state, mgr = env
        mgr.apply_modulation("instincts.exploration", +0.02, "owner")
        old_ts = datetime.now(timezone.utc).timestamp()  # 窗口起点在调制之后
        mgr.apply_modulation("instincts.exploration", +0.02, "owner")
        mgr.rollback(since_ts=old_ts + 10)
        assert state.get("instincts.exploration") == pytest.approx(0.64)  # 窗口内无条目→不动


class TestDampingController:
    def test_consecutive_denials_raise_threshold(self, env):
        _, spine, kernel, state, mgr = env
        damping = DampingController(mgr, spine, consecutive=3, bump=0.05)
        for _ in range(3):
            damping.record_ignored("curiosity_gap")
        spec = state.desires["curiosity_gap"]
        assert spec["threshold"] == pytest.approx(0.65)
        assert spine.find(lambda d: d["event_type"] == "dampen")

    def test_acceptance_resets_counter(self, env):
        _, spine, kernel, state, mgr = env
        damping = DampingController(mgr, spine, consecutive=3, bump=0.05)
        damping.record_ignored("curiosity_gap")
        damping.record_ignored("curiosity_gap")
        damping.record_accepted("curiosity_gap")
        damping.record_ignored("curiosity_gap")   # 计数已重置，未达 3 不上调
        assert state.desires["curiosity_gap"]["threshold"] == pytest.approx(0.6)

    def test_threshold_capped_by_bounds(self, env):
        _, spine, kernel, state, mgr = env
        damping = DampingController(mgr, spine, consecutive=1, bump=0.5)
        state.desires["curiosity_gap"]["threshold"] = 0.9
        damping.record_ignored("curiosity_gap")
        assert state.desires["curiosity_gap"]["threshold"] == pytest.approx(0.95)

    def test_wired_into_approval_queue_denials(self, env, tmp_path):
        from proto.exec.approval import ApprovalQueue
        from proto.exec.workspace import ScratchWorkspace
        _, spine, kernel, state, mgr = env
        damping = DampingController(mgr, spine, consecutive=3, bump=0.05)
        queue = ApprovalQueue(kernel, spine, damping=damping)
        for i in range(3):
            sub = queue.submit(ActionProposal(
                ActionCategory.VISIBLE_WRITE, f"主动提议{i}", {}, task_id=f"t{i}",
                self_initiated=True, desire_dim="curiosity_gap"))
            queue.deny(sub.proposal_id, reason="不需要")
        assert state.desires["curiosity_gap"]["threshold"] == pytest.approx(0.65)
