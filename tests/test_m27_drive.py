# -*- coding: utf-8 -*-
"""M27: 可观察的自主驱动——缺口达标即行动（冷却制）/立即行动按钮/增量式欲望增长。"""
import json
import time

import pytest

from proto.llm.client import LLMClient


class Act(LLMClient):
    def complete(self, system, user):
        if "归因" in system:
            return "[]"
        if "最小可检验定义" in system:
            return json.dumps({"acceptance": "产出带来源笔记"})
        if "独立验收" in system:
            return json.dumps({"verdict": "pass"})
        if "预测" in system:
            return json.dumps([])
        return "好的。"


def dead_web():
    from proto.exec.webtools import WebToolkit
    return WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))


@pytest.fixture()
def agent(tmp_path):
    from proto.runtime.agent import ProtoAgent
    return ProtoAgent(data_dir=tmp_path, llm=Act(), goal_interval_s=10 ** 9,
                      webtools=dead_web())


class TestIncrementalDesireGrowth:
    def test_evolve_incremental_not_recounted(self, tmp_path):
        from proto.runtime.agent import ProtoAgent
        # 语义：同样"闲置 2 小时（宽限后计费 1h）"，一步计费与两步计费结果一致
        a1 = ProtoAgent(data_dir=tmp_path / "one", llm=Act(), goal_interval_s=10 ** 9,
                        webtools=dead_web())
        a1.desires.evolve(1000.0, 1000.0)                    # 建基线
        a1.desires.evolve(1000.0 + 2 * 3600, 1000.0)         # 一步 2h
        one_shot = a1.state.get("desires.curiosity_gap")
        a2 = ProtoAgent(data_dir=tmp_path / "two", llm=Act(), goal_interval_s=10 ** 9,
                        webtools=dead_web())
        a2.desires.evolve(1000.0, 1000.0)                    # 建基线
        a2.desires.evolve(1000.0 + 3600, 1000.0)             # 先 1h
        a2.desires.evolve(1000.0 + 7200, 1000.0)             # 再 1h
        two_step = a2.state.get("desires.curiosity_gap")
        assert one_shot == pytest.approx(two_step, abs=0.001)


class TestActOnGapNotClock:
    def test_gap_over_threshold_acts_without_scheduler_beat(self, agent):
        agent.state.set_within_bounds("desires.curiosity_gap", 0.7)
        agent._last_self_act = 0
        msg = agent.maybe_proactive()
        assert msg and ("目标" in msg or "学习" in msg)

    def test_recent_action_does_not_block(self, agent):
        """M59 新契约：干完活立即允许再干（冷却已废）；只有秒败熔断才挡。
        旧契约（_last_self_act 冷却挡重复）随 M59 废除。"""
        agent.state.set_within_bounds("desires.curiosity_gap", 0.95)
        agent.maybe_proactive()
        agent._last_chain = {"ended": time.time(), "status": "verified"}
        assert agent._may_start_chain() is True
        agent._last_chain = {"ended": time.time(), "status": "failed"}
        assert agent._may_start_chain() is False   # 秒败熔断（防打转）


class TestForceAct:
    def test_force_act_stimulates_and_acts(self, agent):
        result = agent.force_act()
        assert result["acted"] is True
        assert agent.pending_proposals()
        notes = [p for p in agent.workspace.list_scratch() if p.startswith("notes/")]
        assert notes
        assert agent.spine.find(lambda d: d["event_type"] == "force_act")

    def test_force_act_respects_mute(self, agent):
        agent.kernel.set_mute(True)
        result = agent.force_act()
        assert result["acted"] is False and "静音" in result["reason"]
