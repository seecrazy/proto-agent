# -*- coding: utf-8 -*-
"""Task 7b: 认知调制器（设计 §7.4 表逐条）+ 状态注入器。"""
import pytest

from proto.exec.injector import StateInjector
from proto.exec.modulators import (
    exploration_epsilon, initiative_threshold, retrieval_breadth, valence_bias,
)
from proto.state.agent_state import AgentState


class TestModulators:
    # arousal=0.5 → k=base_k（中点不动）
    def test_retrieval_breadth_midpoint(self):
        assert retrieval_breadth(8, 0.5) == 8

    def test_retrieval_breadth_bounds(self):
        assert retrieval_breadth(8, 0.0) == 6   # base-2 下界
        assert retrieval_breadth(8, 1.0) == 10  # round(8*1.3)=10.4→10, 且 ≤ base+3

    # ε = 0.05 + 0.30×exploration×arousal，界 [0.05, 0.35]
    def test_epsilon_formula(self):
        assert exploration_epsilon(0.9, 1.0) == pytest.approx(0.32)
        assert exploration_epsilon(0.0, 1.0) == pytest.approx(0.05)

    def test_epsilon_clamped_upper(self):
        assert exploration_epsilon(1.0, 1.0) == 0.35

    def test_valence_bias(self):
        assert valence_bias(0.5) == pytest.approx(0.1)
        assert valence_bias(-1.0) == pytest.approx(-0.2)
        assert valence_bias(2.0) == 0.2  # clamp

    def test_initiative_threshold_midpoint(self):
        # dominance=0.5 → th = base × (1.1 − 0.2) = 0.9×base
        assert initiative_threshold(0.6, 0.5) == pytest.approx(0.54)

    def test_initiative_threshold_bounds(self):
        # 公式在 dominance=1 给 0.7×base，但硬界 [0.8×base, 1.2×base] 截到 0.48
        assert initiative_threshold(0.6, 1.0) == pytest.approx(0.6 * 0.8)
        assert initiative_threshold(0.6, 0.0) == pytest.approx(0.6 * 1.1)


class TestInjector:
    def _strategies(self):
        from proto.metacog.strategies import StrategyLibrary
        lib = StrategyLibrary()
        lib.add("当任务含外部 API 调用时，先写 mock 再联调", cluster="integration", utility=0.82)
        lib.add("长文本先分段检索再聚合", cluster="retrieval", utility=0.66)
        return lib

    def test_render_contains_state_and_modulator_values(self):
        state = AgentState()
        inj = StateInjector()
        text = inj.render("修复登录超时", state, self._strategies().retrieve("API mock 联调", k=2, epsilon=0.0))
        assert "修复登录超时" in text
        assert "内在状态" in text and "认知调制" in text and "行为策略" in text
        assert "ε=" in text and "k=" in text

    def test_render_marks_over_threshold_desire(self):
        state = AgentState()
        state.desires["curiosity_gap"]["value"] = 0.8  # > threshold 0.6
        text = StateInjector().render("任务", state, [])
        assert "curiosity_gap:0.8(高)" in text.replace(" ", "")

    def test_different_states_render_different_context(self):
        low = AgentState()
        low.instincts["exploration"]["value"] = 0.15
        low.emotion["arousal"] = 0.1
        high = AgentState()
        high.instincts["exploration"]["value"] = 0.85
        high.emotion["arousal"] = 0.95
        inj = StateInjector()
        t1 = inj.render("同一任务", low, [])
        t2 = inj.render("同一任务", high, [])
        assert t1 != t2

    def test_render_is_deterministic(self):
        state = AgentState()
        inj = StateInjector()
        strats = self._strategies().retrieve("API", k=2, epsilon=0.0)
        assert inj.render("T", state, strats) == inj.render("T", state, strats)
