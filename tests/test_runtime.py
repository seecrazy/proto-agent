# -*- coding: utf-8 -*-
"""M5-b: ProtoAgent 运行时——clone 后 `python -m proto.run` 得到的就是它。"""
import json

import pytest

from proto.llm.client import LLMClient
from proto.runtime.agent import ProtoAgent


class ScriptedLLM(LLMClient):
    """按调用顺序返回预设回复：第 1 次给归因 JSON，之后给对话回复。"""
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, user):
        self.calls.append({"system": system, "user": user})
        return self.replies.pop(0)


@pytest.fixture()
def agent(tmp_path):
    llm = ScriptedLLM([
        json.dumps([{"param": "instincts.social_initiative", "direction": 1, "strength": 0.6}]),
        "好的主人，我会更主动一些。",
    ])
    return ProtoAgent(data_dir=tmp_path / "agent", llm=llm)


class TestLifecycle:
    def test_first_run_enrolls_and_persists_owner_key(self, tmp_path):
        llm = ScriptedLLM(["[]", "嗯。"])
        a1 = ProtoAgent(data_dir=tmp_path / "x", llm=llm)
        fp = a1.fingerprint
        assert fp.startswith("sha256:")
        a2 = ProtoAgent(data_dir=tmp_path / "x", llm=llm)
        assert a2.fingerprint == fp          # 密钥持久化，不重复注册
        a1.chat("你好")
        assert a2.spine.verify_chain().ok    # 两个实例共享同一审计链存储

    def test_chat_runs_full_owner_pipeline(self, agent):
        reply = agent.chat("以后更主动一点")
        assert reply == "好的主人，我会更主动一些。"
        # 归因→累积→调制：0.6 ≥ 0.30 触发一次 +0.02
        assert agent.state.get("instincts.social_initiative") == pytest.approx(0.52)
        # LLM 看到的上下文包含注入的内在状态与用户话语
        chat_calls = [c for c in agent.llm.calls if "内在状态" in c["system"]]
        assert chat_calls
        assert "以后更主动一点" in chat_calls[-1]["user"]

    def test_offline_mode_still_modulates_and_replies(self, tmp_path):
        agent = ProtoAgent(data_dir=tmp_path / "offline")  # 无 LLM
        reply = agent.chat("以后更主动一点")
        assert "离线" in reply
        assert agent.state.get("instincts.social_initiative") == pytest.approx(0.52)
        assert "social_initiative" in agent.status()

    def test_status_shows_observable_state(self, agent):
        agent.chat("你好")
        status = agent.status()
        for key in ("exploration", "curiosity_gap", "rigor", "pleasure"):
            assert key in status

    def test_audit_tail(self, agent):
        agent.chat("你好")
        assert agent.audit_tail(5)           # 非空列表


class TestProactive:
    def test_proposes_when_desire_crosses_threshold(self, tmp_path):
        llm = ScriptedLLM(["[]", "嗯。", "[]", "我来整理一份学习笔记。"])
        agent = ProtoAgent(data_dir=tmp_path / "p", llm=llm, goal_interval_s=0)
        # 手动把好奇缺口顶过阈值（测试快捷路径；生产由闲置增长驱动）
        agent.state.set_within_bounds("desires.curiosity_gap", 0.9)
        proposal = agent.maybe_proactive()
        assert proposal is not None and "提案" in proposal
        assert agent.pending_proposals()

    def test_deny_proposal_dampens(self, tmp_path):
        llm = ScriptedLLM(["[]", "嗯。", "[]", "整理笔记。"])
        agent = ProtoAgent(data_dir=tmp_path / "p2", llm=llm, goal_interval_s=0)
        agent.state.set_within_bounds("desires.curiosity_gap", 0.9)
        agent.maybe_proactive()
        pid = agent.pending_proposals()[0]["proposal_id"]
        agent.deny_proposal(pid)
        assert agent.state.desires["curiosity_gap"]["threshold"] == pytest.approx(0.65)

    def test_accept_proposal_executes_draft(self, tmp_path):
        llm = ScriptedLLM(["[]", "嗯。", "[]", "整理笔记。"])
        agent = ProtoAgent(data_dir=tmp_path / "p3", llm=llm, goal_interval_s=0)
        agent.state.set_within_bounds("desires.curiosity_gap", 0.9)
        agent.maybe_proactive()
        pid = agent.pending_proposals()[0]["proposal_id"]
        result = agent.accept_proposal(pid)
        assert result["status"] == "completed"
        assert agent.workspace.list_scratch()   # 草稿落在 scratch，零外部性


class TestTaskExecution:
    def test_owner_task_planned_and_executed(self, tmp_path):
        # M16 起 run_task 走 Agentic 循环：模型自主决定工具调用
        plan = json.dumps([{"tool": "clock", "args": {}, "category": "read_draft"}])
        done = json.dumps({"done": True, "summary": "已获取时间"})
        llm = ScriptedLLM([plan, done])
        agent = ProtoAgent(data_dir=tmp_path / "t", llm=llm)
        result = agent.run_task("看一下现在几点")
        assert result["status"] == "completed"
        assert result["observations"]            # clock 工具真实执行并回灌观察
