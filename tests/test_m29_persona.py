# -*- coding: utf-8 -*-
"""M29: 人设 → 默认内在状态与种子目标。"""
import json

import pytest

from proto.llm.client import LLMClient
from proto.runtime.personas import PERSONA_DEFAULTS


class Pers(LLMClient):
    def __init__(self, reply="好的。"):
        self.reply = reply
    def complete(self, system, user):
        if "归因" in system:
            return "[]"
        return self.reply


def dead_web():
    from proto.exec.webtools import WebToolkit
    return WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))


@pytest.fixture()
def agent(tmp_path):
    from proto.runtime.agent import ProtoAgent
    return ProtoAgent(data_dir=tmp_path, llm=Pers(), goal_interval_s=10 ** 9,
                      webtools=dead_web())


SOFTWARE = "资深软件专家，重视工程质量与可验证性，喜欢把复杂问题拆解清楚"


class TestPresetAppliesState:
    def test_persona_applies_params_and_seed_goal(self, agent):
        agent.set_persona(SOFTWARE)
        assert agent.state.get("personality.rigor") == pytest.approx(0.85)
        assert agent.state.get("instincts.risk_tolerance") == pytest.approx(0.3)
        assert any("软件工程" in g.text for g in agent.goal_tree.active_long_term())

    def test_modulated_params_not_overwritten(self, agent):
        # 反馈已把 exploration 调离出厂值 → 应用预设时保留成长痕迹
        agent.mgr.apply_modulation("instincts.exploration", +0.02, "owner_channel")
        agent.set_persona(SOFTWARE)
        assert agent.state.get("instincts.exploration") == pytest.approx(0.62)

    def test_preset_application_audited(self, agent):
        agent.set_persona(SOFTWARE)
        assert agent.spine.find(lambda d: d["event_type"] == "persona_defaults")


class TestFreeTextPersona:
    def test_llm_generates_recommendation(self, tmp_path):
        llm = Pers(json.dumps({
            "params": {"personality.rigor": 0.8},
            "seed_goal": "成为可靠的 Rust 顾问"}))
        from proto.runtime.agent import ProtoAgent
        a = ProtoAgent(data_dir=tmp_path, llm=llm, goal_interval_s=10 ** 9,
                       webtools=dead_web())
        a.set_persona("Rust 重度爱好者，讨厌不安全的代码")
        assert a.state.get("personality.rigor") == pytest.approx(0.8)
        assert any("Rust" in g.text for g in a.goal_tree.active_long_term())

    def test_offline_keeps_factory_defaults(self, tmp_path):
        from proto.runtime.agent import ProtoAgent
        a = ProtoAgent(data_dir=tmp_path, goal_interval_s=10 ** 9,
                       webtools=dead_web())
        a.set_persona("某个自由描述的人设")
        assert a.state.get("personality.rigor") == pytest.approx(0.7)  # 出厂值
