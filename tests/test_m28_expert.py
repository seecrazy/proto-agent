# -*- coding: utf-8 -*-
"""M28: 专家工作模型——目标牵引优先/晨间扫描/内在状态滑块。"""
import json
import time

import pytest

from proto.llm.client import LLMClient


class Expert(LLMClient):
    def complete(self, system, user):
        if "归因" in system:
            return "[]"
        if "最小可检验定义" in system:
            return json.dumps({"acceptance": "产出可验证结果"})
        if "独立验收" in system:
            return json.dumps({"verdict": "pass"})
        if "分解" in system:
            return json.dumps([{"text": "调研生态", "acceptance": "笔记产出"}])
        if "预测" in system:
            return json.dumps([])
        if "晨间" in system or "trending" in system.lower() or "简报" in system:
            return "## 晨间简报\n- ghidra 安全研究热度上升\n- agent 框架迭代加速"
        return "好的。"


def dead_web():
    from proto.exec.webtools import WebToolkit
    return WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))


@pytest.fixture()
def agent(tmp_path):
    from proto.runtime.agent import ProtoAgent
    return ProtoAgent(data_dir=tmp_path, llm=Expert(), goal_interval_s=10 ** 9,
                      webtools=dead_web())


class TestGoalPulledWork:
    def test_active_goal_drives_action_without_gap(self, agent):
        """核心修正：有活干活——目标树有 active 目标即推进，不等缺口积累。"""
        agent.set_priority("研究插件生态")            # 播种长期+分解出 mid
        agent.state.set_within_bounds("desires.curiosity_gap", 0.31)  # 缺口远低于阈值
        agent._last_self_act = 0                    # 冷却已过
        msg = agent.maybe_proactive()
        assert msg is not None                      # 目标牵引，行动发生

    def test_no_goal_and_no_gap_stays_idle(self, agent):
        for dim in agent.state.desires:             # 压下所有缺口
            agent.state.set_within_bounds(f"desires.{dim}", 0.0)
        agent._last_self_act = 0
        assert agent.maybe_proactive() is None      # 无目标无缺口：待命

    def test_gap_role_demoted_to_new_goal_trigger(self, agent):
        """缺口只管开新方向：无目标但缺口达标 → 生成目标并行动。"""
        for dim in agent.state.desires:
            if dim != "curiosity_gap":
                agent.state.set_within_bounds(f"desires.{dim}", 0.0)
        agent.state.set_within_bounds("desires.curiosity_gap", 0.9)
        agent._last_self_act = 0
        msg = agent.maybe_proactive()
        assert msg is not None                       # 行动发生
        # 缺口行动后缺口被满足消减（内稳态闭环），树中留下模板目标或调查目标
        assert agent.state.get("desires.curiosity_gap") < 0.9


class TestMorningBrief:
    def test_daily_brief_written_once_per_day(self, agent):
        agent._morning_brief()
        briefs = [p for p in agent.workspace.list_scratch()
                  if p.startswith("notes/brief-")]
        assert briefs
        assert any("晨间" in m for m in agent.pop_inbox())
        agent._morning_brief()                       # 同日第二次 → 幂等跳过
        briefs2 = [p for p in agent.workspace.list_scratch()
                   if p.startswith("notes/brief-")]
        assert len(briefs2) == len(briefs)

    def test_brief_uses_real_trending_when_online(self, tmp_path):
        from proto.exec.webtools import WebToolkit
        from proto.runtime.agent import ProtoAgent
        web = WebToolkit(allowlist=("api.github.com", "raw.githubusercontent.com"),
                         fetch=lambda url, accept="text/html": (
                             200, json.dumps({"items": [
                                 {"full_name": "a/hot", "stargazers_count": 100,
                                  "description": "热门项目"}]})))
        a = ProtoAgent(data_dir=tmp_path, llm=Expert(), goal_interval_s=10 ** 9,
                       webtools=web)
        a._morning_brief()
        briefs = [p for p in a.workspace.list_scratch() if p.startswith("notes/brief-")]
        assert briefs
        content = (a.workspace._scratch / briefs[0]).read_text(encoding="utf-8")
        assert "a/hot" in content                    # 真实 trending 数据进了简报


class TestSliderControl:
    def test_state_panel_has_range_inputs(self):
        html = open("proto/web/index.html", encoding="utf-8").read()
        assert 'type="range"' in html
        # 滑块在参数渲染模板中（每个参数运行时各生成一个）
        assert "slideParam" in html and 'onchange="slideParam' in html
