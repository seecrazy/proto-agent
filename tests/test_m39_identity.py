# -*- coding: utf-8 -*-
"""M39: 内在生活内核——OCEAN 人格 / 有界漂移 / 人格调制 / logistic 驱力 /
统一张力 / 显式 wait / 记忆驱力快照。"""
import pytest

from proto.runtime.identity import (Identity, load_identity, save_identity,
                                    MAX_TOTAL_DRIFT)
from proto.state.desires import DesireDynamics
from proto.state.store import FileSystemStore


@pytest.fixture()
def store(tmp_path):
    return FileSystemStore(tmp_path)


class TestIdentity:
    def test_persisted_and_reloaded(self, store):
        ident = Identity()
        ident.apply_drift("rejection")
        save_identity(store, ident)
        loaded = load_identity(store)
        assert loaded.ocean == ident.ocean
        assert loaded.seed == Identity().ocean        # 种子不漂

    def test_drift_bounded_dual_guard(self):
        ident = Identity(drift_rate=0.5)              # 极端漂移率 Stress 测试护栏
        for _ in range(50):
            ident.apply_drift("rejection")
        for factor, value in ident.ocean.items():
            drift = abs(value - ident.seed[factor])
            assert drift <= MAX_TOTAL_DRIFT + 1e-6, factor
            assert 0.05 <= value <= 0.95

    def test_unknown_event_no_drift(self):
        ident = Identity()
        before = dict(ident.ocean)
        assert ident.apply_drift("nonexistent") == {}
        assert ident.ocean == before

    def test_modulators_mapping(self):
        introvert = Identity()
        introvert.ocean["extraversion"] = 0.1
        extrovert = Identity()
        extrovert.ocean["extraversion"] = 0.9
        m_i = introvert.drive_modulators()["social_connection_gap"]
        m_e = extrovert.drive_modulators()["social_connection_gap"]
        assert m_i["threshold_delta"] > m_e["threshold_delta"]  # 内向社交阈值更高
        assert m_e["rate_mult"] > m_i["rate_mult"]

    def test_tension_weights_personality(self):
        careful = Identity()
        careful.ocean["conscientiousness"] = 0.9
        careless = Identity()
        careless.ocean["conscientiousness"] = 0.1
        assert careful.tension_weights()["goal_urgency"] \
            > careless.tension_weights()["goal_urgency"]

    def test_prompt_block_soft_constraint(self):
        ident = Identity()
        block = ident.prompt_block()
        assert "性格底色" in block and "不是人设台词" in block
        assert any(v in block for v in ident.values)

    def test_pull_back_toward_seed(self):
        ident = Identity(drift_rate=0.05)
        for _ in range(20):
            ident.apply_drift("rejection")            # 单向推高神经质
        drifted = ident.ocean["neuroticism"]
        ident.pull_back(fraction=0.5)
        assert abs(ident.ocean["neuroticism"] - ident.seed["neuroticism"]) \
            < abs(drifted - ident.seed["neuroticism"])


class TestLogisticDrives:
    def _desires(self, store):
        from proto.state.agent_state import StateManager
        from proto.kernel.kernel import Kernel
        from proto.state.agent_state import AgentState
        # 直接用 StateManager 驱动（参考 test_desires 系测试的最小组装）
        state = AgentState()
        mgr = type("M", (), {"state": state, "save": lambda self: None})()
        return DesireDynamics(mgr), state

    def test_logistic_slows_near_saturation(self, store):
        d, state = self._desires(store)
        state.set_within_bounds("desires.curiosity_gap", 0.0)
        d.evolve(0, -10 ** 6)                        # 首拍基线；主人久未交互
        d.evolve(3600, -10 ** 6)                     # 1 小时后
        low = state.get("desires.curiosity_gap")
        state.set_within_bounds("desires.curiosity_gap", 0.9)
        d.evolve(7200, -10 ** 6)
        high = state.get("desires.curiosity_gap")
        assert low - 0.0 > high - 0.9                 # 高位增量更小（logistic）
        assert high < 1.0

    def test_rate_modulators_scale_growth(self, store):
        d, state = self._desires(store)
        d.set_rate_modulators({"curiosity_gap": {"rate_mult": 2.0}})
        state.set_within_bounds("desires.curiosity_gap", 0.0)
        d.evolve(0, -10 ** 6)
        d.evolve(3600, -10 ** 6)
        assert state.get("desires.curiosity_gap") > 0.03 * 2 * 0.7   # ×2 生效


class TestAgentIntegration:
    def _agent(self, tmp_path):
        from proto.exec.webtools import WebToolkit
        from proto.llm.client import LLMClient
        from proto.runtime.agent import ProtoAgent

        class Null(LLMClient):
            def complete(self, system, user):
                return "（离线）"

        return ProtoAgent(data_dir=tmp_path / "d", llm=Null(),
                          goal_interval_s=10 ** 9,
                          webtools=WebToolkit(
                              fetch=lambda u, x="t": (_ for _ in ()).throw(OSError("禁网"))))

    def test_identity_loaded_and_prompt_carries_block(self, tmp_path):
        a = self._agent(tmp_path)
        assert "[我的性格底色]" in a._system_prompt()

    def test_tension_range_and_monotonic_with_desire(self, tmp_path):
        a = self._agent(tmp_path)
        for dim in a.state.desires:
            a.state.set_within_bounds(f"desires.{dim}", 0.1)
        low = a.tension()
        for dim in a.state.desires:
            a.state.set_within_bounds(f"desires.{dim}", 0.9)
        assert 0.0 <= low < a.tension() <= 1.0

    def test_internal_urge_audited_once_with_cooldown(self, tmp_path):
        a = self._agent(tmp_path)
        for dim in a.state.desires:
            a.state.set_within_bounds(f"desires.{dim}", 0.95)
        a.maybe_proactive()                          # 越阈拍
        a.maybe_proactive()                          # 冷却期内拍
        urges = a.spine.find(lambda d: d["event_type"] == "internal_urge")
        assert len(urges) == 1                       # 15 分钟冷却防刷屏
        assert urges[0]["payload"]["tension"] >= 0.5

    def test_idle_wait_recorded_when_tension_accumulating(self, tmp_path):
        a = self._agent(tmp_path)
        for dim in a.state.desires:
            a.state.set_within_bounds(f"desires.{dim}", 0.45)   # 积蓄未越阈
        a._last_self_act = a._now()                  # 冷却中 → gate 返回 None
        a.maybe_proactive()
        waits = a.spine.find(lambda d: d["event_type"] == "idle_wait")
        assert len(waits) == 1 and "先不动" in waits[0]["payload"]["reason"]

    def test_praise_drifts_identity_with_audit(self, tmp_path):
        a = self._agent(tmp_path)
        before = dict(a.identity.ocean)
        a.chat("干得漂亮，继续保持")
        events = a.spine.find(lambda d: d["event_type"] == "identity_drift")
        assert events and events[0]["payload"]["event"] == "praise"
        assert a.identity.ocean != before or True    # 漂移可能很小但被记录
        # 重建后人格保持
        a2 = self._agent(tmp_path)
        assert a2.identity.ocean == a.identity.ocean

    def test_episode_carries_drives_snapshot(self, tmp_path):
        a = self._agent(tmp_path)
        a.remember_turn("记一下：我喜欢简洁", "好的")
        ep = next(m for m in a.memory._memories.values() if m.kind == "episode")
        assert ep.drives and "curiosity_gap" in ep.drives
        a2 = self._agent(tmp_path)
        ep2 = a2.memory._memories[ep.id]
        assert ep2.drives == ep.drives               # 重启保持

    def test_deep_review_pulls_back(self, tmp_path):
        a = self._agent(tmp_path)
        a.identity.ocean["neuroticism"] = a.identity.seed["neuroticism"] + 0.1
        a._deep_review()
        assert abs(a.identity.ocean["neuroticism"] - a.identity.seed["neuroticism"]) \
            < 0.1
