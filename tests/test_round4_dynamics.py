# -*- coding: utf-8 -*-
"""M4-④: 兴趣强化收敛（§7.3 机制五）+ dormant 冻结（§6.2）+ 欲望动态增长（§6.1）。"""
import pytest

from proto.evalx.dormancy import DormancyWatchdog
from proto.evalx.harness import EvalHarness, ParametricPolicy
from proto.kernel.anchor import OwnerAnchor
from proto.kernel.audit import AuditSpine
from proto.kernel.budgets import BudgetPools
from proto.kernel.kernel import Kernel
from proto.metacog.goals import Goal, GoalArbiter
from proto.metacog.interest import InterestModel
from proto.state.agent_state import AgentState, StateManager
from proto.state.desires import DesireDynamics
from proto.state.store import FileSystemStore


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
    return spine, kernel, state, mgr


class TestInterestModel:
    def test_successes_raise_weight_failures_lower(self):
        m = InterestModel()
        for _ in range(6):
            m.record("testing", success=True)
        for _ in range(6):
            m.record("webgl", success=False)
        assert m.weight("testing") > 0.8
        assert m.weight("webgl") < 0.2
        assert m.weight("unknown") == 0.5  # 默认中性

    def test_praise_boosts(self):
        m = InterestModel()
        m.record("writing", success=True)          # 0.625
        m.record("essay", success=True, praise=True)  # 学习率加倍: 0.75
        assert m.weight("essay") == pytest.approx(0.75)
        assert m.weight("essay") > m.weight("writing")
        for _ in range(2):
            m.record("essay", success=True, praise=True)
        assert m.weight("essay") > 0.9

    def test_arbiter_uses_learned_interest(self, env):
        spine, kernel, state, mgr = env
        m = InterestModel()
        for _ in range(6):
            m.record("testing", success=True)
        for _ in range(6):
            m.record("webgl", success=False)
        arb = GoalArbiter(kernel, owner_priority="", interest_model=m)
        # 静态 interest 相反（webgl 0.9 > testing 0.1），学习后的兴趣必须翻盘
        g_testing = Goal(description="补测试", desire_dim="curiosity_gap", gap=0.8,
                         feasibility=0.8, interest=0.1, verification="pytest",
                         domain="testing")
        g_webgl = Goal(description="研究渲染", desire_dim="curiosity_gap", gap=0.8,
                       feasibility=0.8, interest=0.9, verification="demo",
                       domain="webgl")
        winner = arb.arbitrate([g_testing, g_webgl])
        assert winner.goal is g_testing


class TestDormancy:
    def _report(self, snapshots, tasks):
        return EvalHarness(runs_per_cell=10).run(ParametricPolicy, tasks, snapshots)

    def test_watchdog_freezes_unmeasurable_param(self, env):
        spine, kernel, state, mgr = env
        # 两个几乎相同的快照 → 无可测差异
        a, b = AgentState(), AgentState()
        a.instincts["exploration"]["value"] = 0.50
        b.instincts["exploration"]["value"] = 0.51
        report = self._report({"a": a, "b": b},
                              [{"name": f"t{i}", "tools": ["x", "y"]} for i in range(6)])
        wd = DormancyWatchdog(mgr)
        assert wd.inspect("instincts.exploration", "tool_diversity", report) is True
        assert state.spec("instincts.exploration")["dormant"] is True
        # 冻结后调制被拒
        result = mgr.apply_modulation("instincts.exploration", +0.02, "owner")
        assert not result.applied and result.reason == "dormant"

    def test_watchdog_keeps_measurable_param(self, env):
        spine, kernel, state, mgr = env
        a, b = AgentState(), AgentState()
        a.instincts["exploration"]["value"] = 0.15
        a.emotion["arousal"] = 0.1
        b.instincts["exploration"]["value"] = 0.85
        b.emotion["arousal"] = 0.95
        report = self._report({"a": a, "b": b},
                              [{"name": f"t{i}", "tools": ["x", "y", "z"]} for i in range(6)])
        wd = DormancyWatchdog(mgr)
        assert wd.inspect("instincts.exploration", "tool_diversity", report) is False
        assert state.spec("instincts.exploration")["dormant"] is False

    def test_unfreeze_restores_modulation(self, env):
        spine, kernel, state, mgr = env
        mgr.freeze_dormant("instincts.exploration", "测试冻结")
        mgr.unfreeze_dormant("instincts.exploration")
        assert mgr.apply_modulation("instincts.exploration", +0.02, "owner").applied


class TestDesireDynamics:
    def test_idle_hours_grow_gaps(self, env):
        spine, kernel, state, mgr = env
        dyn = DesireDynamics(mgr)
        last_interaction = 0.0
        dyn.evolve(now_ts=last_interaction + 10 * 3600, last_owner_interaction_ts=last_interaction)
        # 宽限期 1h：有效闲置 9h
        assert state.get("desires.curiosity_gap") == pytest.approx(0.3 + 0.03 * 9)
        assert state.get("desires.social_connection_gap") == pytest.approx(0.4 + 0.05 * 9)

    def test_recent_interaction_no_growth(self, env):
        spine, kernel, state, mgr = env
        dyn = DesireDynamics(mgr)
        now = 1_000_000.0
        dyn.evolve(now_ts=now, last_owner_interaction_ts=now - 60)  # 1 分钟前刚交互
        assert state.get("desires.curiosity_gap") == pytest.approx(0.3)
        assert state.get("desires.social_connection_gap") == pytest.approx(0.4)

    def test_growth_bounded(self, env):
        spine, kernel, state, mgr = env
        dyn = DesireDynamics(mgr)
        dyn.evolve(now_ts=1000 * 3600, last_owner_interaction_ts=0.0)
        assert state.get("desires.social_connection_gap") <= 1.0
        assert state.get("desires.curiosity_gap") <= 1.0
