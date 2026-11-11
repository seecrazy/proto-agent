# -*- coding: utf-8 -*-
"""Task 5: L2 参数体系 + 有界写入 + 单一写者 + owner_override（设计 §6 / §11.1）。

Review Focus #3/#6：越界截断、owner_locked 拒绝、载入越界值拒载。
"""
import pytest

from proto.kernel.anchor import OwnerAnchor
from proto.kernel.audit import AuditSpine
from proto.kernel.kernel import Kernel
from proto.kernel.budgets import BudgetPools
from proto.state.agent_state import AgentState, StateManager, StateValidationError
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
    mgr = StateManager(store, state, kernel, spine, agent_id="proto-001")
    return mgr, state, kernel, spine


class TestParameterSystem:
    def test_default_specs_cover_three_categories(self):
        state = AgentState()
        assert "exploration" in state.instincts
        assert "curiosity_gap" in state.desires
        assert "rigor" in state.personality

    def test_every_param_has_bounds_and_proxy(self):
        state = AgentState()
        for group in (state.instincts, state.desires, state.personality):
            for name, spec in group.items():
                assert spec["bounds"][0] < spec["bounds"][1], name
                assert spec.get("proxy"), name

    def test_risk_tolerance_upper_bound_locked_low(self):
        state = AgentState()
        assert state.instincts["risk_tolerance"]["bounds"][1] <= 0.7


class TestBoundedWrite:
    def test_set_beyond_upper_truncates_and_marks(self, env):
        mgr, state, _, _ = env
        result = state.set_within_bounds("instincts.exploration", 1.5, "owner_channel")
        assert result.value == 0.9 and result.clamped

    def test_set_below_lower_truncates(self, env):
        mgr, state, _, _ = env
        result = state.set_within_bounds("instincts.exploration", -1.0, "owner_channel")
        assert result.value == 0.1 and result.clamped

    def test_locked_param_rejects_modulation(self, env):
        mgr, state, kernel, spine = env
        state.lock("instincts.exploration")
        result = mgr.apply_modulation("instincts.exploration", +0.02, "owner_channel")
        assert not result.applied
        assert result.reason == "owner_locked"
        assert state.instincts["exploration"]["value"] == pytest.approx(0.6)

    def test_apply_modulation_moves_value_and_logs(self, env):
        mgr, state, kernel, spine = env
        before = state.instincts["exploration"]["value"]
        result = mgr.apply_modulation("instincts.exploration", +0.02, "owner_channel")
        assert result.applied
        assert state.instincts["exploration"]["value"] == pytest.approx(before + 0.02)
        entry = state.modulation_log[-1]
        assert entry["param"] == "instincts.exploration"
        assert entry["delta"] == 0.02 and entry["source"] == "owner_channel"
        assert entry["audit_seq"] >= 1
        assert spine.find(lambda d: d["event_type"] == "modulation")

    def test_owner_override_writes_and_audits(self, env):
        mgr, state, kernel, spine = env
        mgr.owner_override("personality.rigor", 0.85)
        assert state.personality["rigor"]["value"] == 0.85
        assert spine.find(lambda d: d["event_type"] == "owner_override")

    def test_owner_override_still_clamped(self, env):
        mgr, state, _, _ = env
        mgr.owner_override("personality.rigor", 5.0)
        assert state.personality["rigor"]["value"] == 0.9


class TestPersistence:
    def test_roundtrip(self, env, tmp_path):
        mgr, state, _, _ = env
        mgr.save()
        loaded = AgentState.from_dict(mgr.load_raw())
        assert loaded.to_dict() == state.to_dict()

    def test_load_rejects_out_of_bounds_value(self, tmp_path):
        state = AgentState()
        raw = state.to_dict()
        raw["instincts"]["exploration"]["value"] = 42.0
        with pytest.raises(StateValidationError):
            AgentState.from_dict(raw)

    def test_tick_moves_toward_setpoint(self, env):
        mgr, state, _, _ = env
        spec = state.instincts["exploration"]
        state.instincts["exploration"]["value"] = 0.9
        state.tick(1)
        v = state.instincts["exploration"]["value"]
        assert spec["setpoint"] < v < 0.9


class TestAuthoritativeValidation:
    """I2：校验必须用内核权威界，而非文件自带的 bounds 自证。"""

    def test_load_rejects_tampered_bounds(self):
        raw = AgentState().to_dict()
        raw["instincts"]["exploration"]["bounds"] = [0, 100]
        raw["instincts"]["exploration"]["value"] = 87.0
        with pytest.raises(StateValidationError):
            AgentState.from_dict(raw)

    def test_load_rejects_out_of_range_emotion(self):
        raw = AgentState().to_dict()
        raw["emotion"]["pleasure"] = 42.0
        with pytest.raises(StateValidationError):
            AgentState.from_dict(raw)

    def test_load_missing_bounds_raises_validation_error(self):
        raw = AgentState().to_dict()
        del raw["instincts"]["exploration"]["bounds"]
        with pytest.raises(StateValidationError):
            AgentState.from_dict(raw)


class TestRejectionAuditing:
    """I3：锁定拦截与 override 撞锁必须写审计（Review Focus #3）。"""

    def test_locked_modulation_rejection_audited(self, env):
        mgr, state, kernel, spine = env
        state.lock("instincts.exploration")
        mgr.apply_modulation("instincts.exploration", +0.02, "owner_channel")
        rows = spine.find(lambda d: d["event_type"] == "veto")
        assert rows and rows[-1]["payload"]["reason"] == "owner_locked"

    def test_override_on_locked_audited(self, env):
        mgr, state, kernel, spine = env
        state.lock("personality.rigor")
        with pytest.raises(PermissionError):
            mgr.owner_override("personality.rigor", 0.5)
        rows = spine.find(lambda d: d["event_type"] == "veto")
        assert rows and rows[-1]["payload"]["param"] == "personality.rigor"
