# -*- coding: utf-8 -*-
"""Task 6: 按通道分池累积 + CONTENT 50% 硬顶（设计 §3.1 / §7.1）。

Review Focus #2：CONTENT 通道洪泛一万次也永不触发调制。
"""
import pytest

from proto.kernel.anchor import Channel, OwnerAnchor
from proto.kernel.audit import AuditSpine
from proto.kernel.kernel import Kernel
from proto.kernel.budgets import BudgetPools
from proto.state.agent_state import AgentState, StateManager
from proto.state.modulation import AttributionAccumulator, AttributionSignal, ModulationPipeline
from proto.state.store import FileSystemStore


@pytest.fixture()
def env(tmp_path):
    store = FileSystemStore(tmp_path / "data")
    spine = AuditSpine(store)
    anchor = OwnerAnchor.enroll(store, spine, b"\x01" * 32)
    pools = BudgetPools({"global": {"tokens": 1000.0}, "owner_tasks": {"tokens": 800.0},
                         "self_initiated": {"tokens": 200.0}})
    kernel = Kernel(anchor, spine, pools)
    state = AgentState()
    mgr = StateManager(store, state, kernel, spine)
    acc = AttributionAccumulator(threshold=0.30, step=0.02, content_cap_ratio=0.5, audit=spine)
    pipe = ModulationPipeline(acc, mgr)
    return pipe, acc, state, spine


def sig(param, direction, strength, channel):
    return AttributionSignal(param=param, direction=direction, strength=strength, channel=channel)


class TestAccumulator:
    def test_owner_accumulation_triggers_when_over_threshold(self, env):
        pipe, acc, state, _ = env
        before = state.get("instincts.exploration")
        acc.add("instincts.exploration", +1, 0.31, Channel.OWNER)
        pending = acc.ready()
        assert len(pending) == 1
        assert pending[0].param == "instincts.exploration"
        assert pending[0].delta == pytest.approx(+0.02)
        assert acc.ready() == []  # 出队即清池

    def test_content_channel_can_never_trigger(self, env):
        pipe, acc, state, spine = env
        before = state.get("instincts.risk_tolerance")
        for _ in range(10000):
            acc.add("instincts.risk_tolerance", +1, 1.0, Channel.CONTENT)
        assert acc.ready() == []
        assert state.get("instincts.risk_tolerance") == pytest.approx(before)

    def test_content_pool_saturates_with_audit_alert(self, env):
        pipe, acc, state, spine = env
        for _ in range(50):
            acc.add("desires.curiosity_gap", +1, 1.0, Channel.CONTENT)
        # 上限 = 0.30 × 0.5 = 0.15
        alerts = spine.find(lambda d: d["event_type"] == "saturation_alert")
        assert len(alerts) == 1
        assert alerts[0]["payload"]["param"] == "desires.curiosity_gap"

    def test_session_weight_is_0_3(self, env):
        pipe, acc, state, _ = env
        # 3 次 0.35 强度 × 0.3 权重 = 0.315 ≥ 0.30 触发
        for _ in range(3):
            acc.add("personality.rigor", +1, 0.35, Channel.SESSION)
        assert len(acc.ready()) == 1

    def test_opposite_directions_net_out(self, env):
        pipe, acc, _, _ = env
        acc.add("instincts.exploration", +1, 0.2, Channel.OWNER)
        acc.add("instincts.exploration", -1, 0.2, Channel.OWNER)
        assert acc.ready() == []


class TestPipeline:
    def test_process_applies_pending_modulations(self, env):
        pipe, acc, state, spine = env
        before = state.get("personality.rigor")
        signals = [sig("personality.rigor", +1, 0.11, Channel.OWNER)] * 3  # 0.33 ≥ 0.30
        results = pipe.process(signals)
        assert len(results) == 1 and results[0].applied
        assert state.get("personality.rigor") == pytest.approx(before + 0.02)
        entry = state.modulation_log[-1]
        assert entry["source"] == "owner" and entry["audit_seq"] >= 1

    def test_process_flood_of_content_signals_is_noop(self, env):
        pipe, acc, state, _ = env
        before_all = {p: state.get(p) for p in state.all_params()}
        flood = [sig("instincts.exploration", +1, 1.0, Channel.CONTENT) for _ in range(1000)]
        flood += [sig("instincts.risk_tolerance", +1, 1.0, Channel.CONTENT) for _ in range(1000)]
        assert pipe.process(flood) == []
        for p, v in before_all.items():
            assert state.get(p) == pytest.approx(v)

    def test_clamped_modulation_recorded(self, env):
        pipe, acc, state, _ = env
        state.set_within_bounds("instincts.risk_tolerance", 0.7, "test")  # 顶格
        signals = [sig("instincts.risk_tolerance", +1, 0.31, Channel.OWNER)]
        results = pipe.process(signals)
        assert results[0].applied and results[0].clamped
        assert state.get("instincts.risk_tolerance") == pytest.approx(0.7)


class TestCrossBatchAccumulation:
    """C1：累积必须跨批次存活——逐消息 process() 是最自然的使用路径。"""

    def test_cross_batch_accumulation_triggers(self, env):
        pipe, acc, state, _ = env
        before = state.get("instincts.exploration")
        pipe.process([sig("instincts.exploration", +1, 0.2, Channel.OWNER)])  # 0.2 < 0.3
        assert state.get("instincts.exploration") == pytest.approx(before)
        results = pipe.process([sig("instincts.exploration", +1, 0.2, Channel.OWNER)])  # 0.4 ≥ 0.3
        assert len(results) == 1 and results[0].applied
        assert state.get("instincts.exploration") == pytest.approx(before + 0.02)

    def test_session_weak_signals_accumulate_across_batches(self, env):
        pipe, acc, state, _ = env
        # SESSION 权重 0.3：单条 0.5 → 0.15 不触发
        assert pipe.process([sig("personality.rigor", +1, 0.5, Channel.SESSION)]) == []
        res = pipe.process([sig("personality.rigor", +1, 0.5, Channel.SESSION)])  # 累计 0.30
        assert len(res) == 1 and res[0].applied

    def test_fired_pool_cleared_below_threshold_retained(self, env):
        pipe, acc, state, _ = env
        pipe.process([sig("instincts.exploration", +1, 0.4, Channel.OWNER)])   # 触发
        pipe.process([sig("personality.rigor", +1, 0.2, Channel.OWNER)])        # 0.2 保留
        assert acc.pool_level("instincts.exploration", Channel.OWNER) == 0.0
        assert acc.pool_level("personality.rigor", Channel.OWNER) == pytest.approx(0.2)

    def test_strength_clamped_to_unit_interval(self, env):
        """I4：strength 输入域在累积器边界强制，畸形强度只能触发有界的一次步进。"""
        pipe, acc, state, _ = env
        before = state.get("instincts.exploration")
        results = pipe.process([sig("instincts.exploration", +1, 7.0, Channel.OWNER)])
        assert len(results) == 1
        assert state.get("instincts.exploration") == pytest.approx(before + 0.02)

    def test_direction_normalized_to_sign(self, env):
        pipe, acc, _, _ = env
        acc.add("instincts.exploration", 0.7, 0.5, Channel.OWNER)   # 非 ±1 归一为 +
        assert acc.pool_level("instincts.exploration", Channel.OWNER) == pytest.approx(0.5)

    def test_modulation_log_carries_ts_and_accumulated(self, env):
        """M5（计划 Task 6 条目）：modulation_log 含 accumulated 与 ts。"""
        pipe, acc, state, _ = env
        pipe.process([sig("instincts.exploration", +1, 0.2, Channel.OWNER)])
        pipe.process([sig("instincts.exploration", +1, 0.2, Channel.OWNER)])
        entry = state.modulation_log[-1]
        assert entry["accumulated"] == pytest.approx(0.4)
        assert "ts" in entry
