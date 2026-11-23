# -*- coding: utf-8 -*-
"""M20: L3 三支柱——目标树/世界模型/预测误差驱动的自主目标生成。"""
import json
import time

import pytest

from proto.llm.client import LLMClient
from proto.runtime.agent import ProtoAgent


class L3LLM(LLMClient):
    """脚本化：归因[]/对话/分解/预测/复核判定。"""
    def __init__(self, scripts=None):
        self.scripts = dict(scripts or {})
        self.users = []
    def complete(self, system, user):
        self.users.append(f"{system[:10]}|{user[:80]}")
        if "归因" in system:
            return "[]"
        for key in sorted(self.scripts, key=len, reverse=True):  # 最长键优先
            if key in system:
                return self.scripts[key]
        return "好的。"


def dead_web(fetch_map=None):
    from proto.tools.webtools import WebToolkit
    def fetch(url, accept="text/html"):
        if fetch_map:
            for frag, body in fetch_map.items():
                if frag in url:
                    return 200, body
        raise OSError("禁网")
    return WebToolkit(fetch=fetch)


@pytest.fixture()
def agent(tmp_path):
    return ProtoAgent(data_dir=tmp_path,
                      llm=L3LLM({"分解": json.dumps([
                          {"text": "调研 GPU 供应链"}, {"text": "跟踪大厂资本开支"}]),
                          "预测": json.dumps([
                              {"statement": "模型上下文成本一年内再降 50%",
                               "days_ahead": 30, "confidence": 0.7}])}),
                      goal_interval_s=10 ** 9, webtools=dead_web())


class TestGoalTree:
    def test_seed_and_persist(self, agent):
        agent.set_priority("深入研究 AI 产业")
        longs = agent.goal_tree.active_long_term()
        assert longs and "AI 产业" in longs[0].text
        from proto.goals.system import GoalTree
        tree2 = GoalTree(agent.spine.store)
        assert tree2.active_long_term()          # 重启不失

    def test_complete_propagates_progress_and_milestone(self, agent):
        agent.set_priority("深入研究 AI 产业")     # 播种+分解出 2 个 mid + 自动交付
        long_id = agent.goal_tree.active_long_term()[0].id
        mids = agent.goal_tree.children_of_level(long_id, "mid")
        assert len(mids) == 3
        assert "交付" in mids[-1].text
        before = agent.state.emotion["pleasure"]
        agent.goal_tree.complete(mids[0].id)
        longs = agent.goal_tree.active_long_term()
        assert longs[0].progress == pytest.approx(1 / 3, abs=1e-4)
        assert agent.state.emotion["pleasure"] > before      # 里程碑→情绪
        assert any("里程碑" in m.text for m in agent.memory.episodes())

    def test_select_executable_prefers_deep_and_aligned(self, agent):
        # 直接构造树，隔离选优逻辑本身
        agent.state.owner_priority = "深入研究 AI 产业"
        agent.goal_tree.add("深入研究 AI 产业", level="long")
        agent.goal_tree.add("无关琐事", level="task", source="owner")
        agent.goal_tree.add("整理 AI 产业上游供给", level="task", source="self")
        picked = agent.goal_tree.select_executable("深入研究 AI 产业")
        assert picked.text == "整理 AI 产业上游供给"   # 对齐的 task 胜出
        assert picked.text != "无关琐事"                # 无关项永不选中


class TestWorldModel:
    def test_facts_and_predictions_lifecycle(self, agent):
        agent.world.add_fact("GPU 供应链", "HBM 供给紧张", "github.com/x")
        pid = agent.world.record_prediction("GPU 供应链", "HBM 半年内缓解",
                                            deadline_ts=time.time() - 1)  # 已到期
        due = agent.world.due_predictions()
        assert any(p["id"] == pid for p in due)
        agent.world.settle(pid, "wrong")
        preds = agent.world.snapshot()["topics"]["GPU 供应链"]["predictions"]
        assert preds[0]["status"] == "wrong"

    def test_study_writes_facts_and_predictions(self, tmp_path):
        a = ProtoAgent(data_dir=tmp_path / "x", goal_interval_s=0,
                       llm=L3LLM({"预测": json.dumps([
                           {"statement": "该库半年内破万星", "days_ahead": 180,
                            "confidence": 0.6}])}),
                       webtools=dead_web({
                           "/search/": json.dumps({"items": [
                               {"full_name": "o/lib", "stargazers_count": 1,
                                "description": ""}]}),
                           "HEAD/README.md": "A library about agents."}))
        a.state.set_within_bounds("desires.curiosity_gap", 0.9)
        a.maybe_proactive()
        snap = a.world.snapshot()
        assert any(snap["topics"][t]["facts"] for t in snap["topics"])
        assert any(p["status"] == "open"
                   for t in snap["topics"].values() for p in t["predictions"])


class TestPredictionErrorLoop:
    def test_wrong_prediction_spawns_investigation_goal(self, tmp_path):
        scripts = {
            "预测": json.dumps([{"statement": "该库月增千星", "days_ahead": 1,
                                 "confidence": 0.8}]),
            "预测复核器": json.dumps({"verdict": "wrong", "evidence": "星标几乎未涨"}),
        }
        a = ProtoAgent(data_dir=tmp_path / "y", goal_interval_s=0,
                       llm=L3LLM(scripts),
                       webtools=dead_web({
                           "/search/": json.dumps({"items": [
                               {"full_name": "o/lib", "stargazers_count": 5,
                                "description": ""}]}),
                           "HEAD/README.md": "same readme"}))
        a.set_priority("研究 agent 生态")
        a.state.set_within_bounds("desires.curiosity_gap", 0.9)
        a.maybe_proactive()                      # 学习+记录预测（deadline 1 天）
        # 时间前进使预测到期
        a.world._advance_all(2 * 86400)
        before = a.state.get("desires.curiosity_gap")
        a._review_predictions()
        assert a.state.get("desires.curiosity_gap") > before   # 预测误差→好奇上抬
        goals = [g for g in a.goal_tree.all_active()
                 if "调查" in g.text]
        assert goals and goals[0].source == "self"             # 自主生成目标
        assert any("预测落空" in m for m in a.pop_inbox())

    def test_proactive_uses_tree_goal_not_template(self, tmp_path):
        a = ProtoAgent(data_dir=tmp_path / "z", goal_interval_s=0,
                       llm=L3LLM(), webtools=dead_web({
                           "/search/": json.dumps({"items": [
                               {"full_name": "o/lib", "stargazers_count": 1,
                                "description": ""}]}),
                           "HEAD/README.md": "readme"}))
        a.set_priority("深入研究 AI 产业")
        a.state.set_within_bounds("desires.curiosity_gap", 0.9)
        msg = a.maybe_proactive()
        assert "AI 产业" in msg                                  # 来自目标树
