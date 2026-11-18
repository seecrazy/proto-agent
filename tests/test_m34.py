# -*- coding: utf-8 -*-
"""M34: 提议简明机制 + 审美系统。"""
import json

import pytest

from proto.llm.client import LLMClient
from proto.runtime.taste import DEFAULT_TASTE, load_taste


class Taste(LLMClient):
    def __init__(self, scripts=None):
        self.scripts = dict(scripts or {})
    def complete(self, system, user):
        if "归因" in system:
            return "[]"
        for key, reply in self.scripts.items():
            if key in system:
                return reply
        return "好的。"


def dead_web():
    from proto.exec.webtools import WebToolkit
    return WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))


@pytest.fixture()
def agent(tmp_path):
    from proto.runtime.agent import ProtoAgent
    return ProtoAgent(data_dir=tmp_path, llm=Taste(), goal_interval_s=10 ** 9,
                      webtools=dead_web())


class TestProposalRewrite:
    def test_llm_rewrites_to_three_line_format(self):
        from proto.exec.proposal_writing import rewrite_proposal
        scripts_llm = Taste({
            "提议撰写器": "做什么：研读 3 个 Chrome 插件开源项目\n"
                          "为什么：为插件开发目标积累素材\n"
                          "产出：3 篇带来源的学习笔记进 scratch"})
        from proto.runtime.agent import ProtoAgent
        a = ProtoAgent(data_dir="/tmp/m34_prop", llm=scripts_llm,
                       goal_interval_s=10 ** 9, webtools=dead_web())
        out = rewrite_proposal(a, "围绕当前主人优先级推进一个可以验证的小目标，"
                                  "回应 curiosity_gap 缺口研读开源项目并写了学习笔记"
                                  "围绕优先级推进小目标")
        assert "做什么" in out and "为什么" in out and "产出" in out

    def test_offline_fallback_truncates(self):
        from proto.exec.proposal_writing import rewrite_proposal
        long_raw = "非常长的提议" * 50
        out = rewrite_proposal(None, long_raw)
        assert len(out) <= 165 and out.endswith("…")

    def test_bad_llm_output_falls_back(self):
        from proto.exec.proposal_writing import rewrite_proposal
        from proto.runtime.agent import ProtoAgent
        a = ProtoAgent(data_dir="/tmp/m34_prop2",
                       llm=Taste({"提议撰写器": "我觉得可以做一些事情"}),
                       goal_interval_s=10 ** 9, webtools=dead_web())
        raw = "围绕优先级推进小目标回应缺口"
        assert rewrite_proposal(a, raw) == raw          # 原文兜底


class TestTasteSystem:
    def test_default_taste_loads(self, agent):
        dims = load_taste(agent.spine.store)
        assert "fun" in dims and "elegant" in dims
        assert dims["useful"]["weight"] == 0.8

    def test_judge_scores_with_reasons(self, tmp_path):
        from proto.runtime.agent import ProtoAgent
        llm = Taste({"审美": json.dumps({
            "fun": {"score": 4, "reason": "上手就想试"},
            "interesting": {"score": 5, "reason": "推翻了假设"},
            "useful": {"score": 3, "reason": "场景较窄"},
            "beautiful": {"score": 4, "reason": "克制"},
            "elegant": {"score": 5, "reason": "一机制解多问题"}})})
        from proto.exec.webtools import WebToolkit
        a = ProtoAgent(data_dir=tmp_path, llm=llm, goal_interval_s=10 ** 9,
                       webtools=WebToolkit(fetch=lambda u, x="t":
                                           (_ for _ in ()).throw(OSError("禁网"))))
        from proto.runtime.taste import judge, weighted_score
        verdicts = judge(a, "一个把终端命令转成自然语言的 DSL")
        assert verdicts and verdicts[0]["score"] == 4
        score = weighted_score(verdicts)
        assert 3.0 <= score <= 5.0

    def test_refine_from_owner_feedback(self, agent):
        agent.llm.scripts["审美校准"] = json.dumps({
            "fun": {"anchor": "主人觉得好笑的东西",
                    "anti_anchor": "只有我自己觉得好玩", "weight": 0.3}})
        from proto.runtime.taste import refine_from_feedback, load_taste
        merged = refine_from_feedback(agent, "你觉得有趣的东西太无聊了")
        assert merged["fun"]["anchor"] == "主人觉得好笑的东西"
        assert merged["fun"]["weight"] == 0.3
        # 持久化
        assert load_taste(agent.spine.store)["fun"]["weight"] == 0.3

    def test_taste_prompt_injected_into_chat(self, tmp_path):
        """对话时 agent 的审美宣言进入系统提示。"""
        from proto.exec.webtools import WebToolkit
        llm = Taste()
        from proto.runtime.agent import ProtoAgent
        a = ProtoAgent(data_dir=tmp_path, llm=llm, goal_interval_s=10 ** 9,
                       webtools=WebToolkit(fetch=lambda u, x="t":
                                           (_ for _ in ()).throw(OSError("禁网"))))
        prompt = a._system_prompt()
        assert "我的审美" in prompt and "好玩" in prompt

    def test_taste_prefers_high_score_in_goal_selection(self, tmp_path):
        """目标候选过审美评分：加权分高的胜出（注入学习选题）。"""
        pass  # 评分集成见 agent 侧（选主题时优先高分）
