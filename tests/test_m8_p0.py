# -*- coding: utf-8 -*-
"""M8-A(P0): 元认知持久化（重启不失忆）+ 情绪事件接线。"""
import json

import pytest

from proto.llm.client import LLMClient
from proto.runtime.agent import ProtoAgent


class ScriptedLLM(LLMClient):
    def __init__(self, replies=None):
        self.replies = list(replies or [])
    def complete(self, system, user):
        if "归因" in system:
            return self.replies.pop(0) if self.replies else "[]"
        return self.replies.pop(0) if self.replies else "好的。"


def test_strategy_library_roundtrip():
    from proto.metacog.strategies import StrategyLibrary
    lib = StrategyLibrary()
    lib.add("先写 mock 再联调", cluster="integration", utility=0.8)
    lib2 = StrategyLibrary.from_dict(lib.to_dict())
    assert lib2.retrieve("mock 联调", k=1, epsilon=0.0)[0].text == "先写 mock 再联调"


def test_interest_model_roundtrip():
    from proto.metacog.interest import InterestModel
    m = InterestModel()
    for _ in range(4):
        m.record("testing", success=True)
    m2 = InterestModel.from_dict(m.to_dict())
    assert m2.weight("testing") == m.weight("testing")


def test_damping_roundtrip():
    from proto.metacog.damping import DampingController
    from proto.state.agent_state import AgentState
    d = DampingController.__new__(DampingController)
    d._ignored = {"curiosity_gap": 2}
    d2 = DampingController.__new__(DampingController)
    d2._ignored = {}
    DampingController.restore(d2, d.to_dict())
    assert d2._ignored == {"curiosity_gap": 2}


def test_scheduler_roundtrip():
    from proto.metacog.scheduler import MetacogScheduler
    s = MetacogScheduler(goal_interval_s=100)
    s._last_goal, s._last_deep = 111.0, 222.0
    s2 = MetacogScheduler.from_dict(s.to_dict(), goal_interval_s=100)
    assert s2._last_goal == 111.0 and s2._last_deep == 222.0


class TestRuntimePersistence:
    def test_metacog_survives_restart(self, tmp_path):
        llm = ScriptedLLM()
        a1 = ProtoAgent(data_dir=tmp_path, llm=llm, goal_interval_s=10 ** 9)
        a1.library.add("当涉及外部 API 时先写 mock", cluster="integration", utility=0.8)
        for _ in range(3):
            a1.interest.record("testing", success=True)
        a1.damping._ignored["curiosity_gap"] = 2
        a1.instruct("涉及支付改动必须先写测试")
        a1._persist_metacog()

        a2 = ProtoAgent(data_dir=tmp_path, llm=ScriptedLLM(), goal_interval_s=10 ** 9)
        assert a2.library.retrieve("外部 API mock", k=1, epsilon=0.0)
        assert a2.interest.weight("testing") == pytest.approx(a1.interest.weight("testing"))
        assert a2.damping._ignored.get("curiosity_gap") == 2
        assert len(a2.ledger.internal_rules()) == 0 and len(a2.ledger.get_all()) == 1

    def test_metacog_autosaved_on_mutation(self, tmp_path):
        a1 = ProtoAgent(data_dir=tmp_path, llm=ScriptedLLM(), goal_interval_s=10 ** 9)
        a1.library.add("策略X", cluster="x")          # 应触发自动落盘
        a2 = ProtoAgent(data_dir=tmp_path, llm=ScriptedLLM(), goal_interval_s=10 ** 9)
        assert a2.library.retrieve("策略X", k=1, epsilon=0.0)


class TestEmotionWiring:
    def test_task_success_lifts_pleasure(self, tmp_path):
        plan = json.dumps([{"tool": "clock", "args": {}, "category": "read_draft"}])
        agent = ProtoAgent(data_dir=tmp_path, llm=ScriptedLLM([plan]),
                           goal_interval_s=10 ** 9)
        before = agent.state.emotion["pleasure"]
        agent.run_task("看时间")
        assert agent.state.emotion["pleasure"] > before

    def test_denial_drops_pleasure_and_dominance(self, tmp_path):
        agent = ProtoAgent(data_dir=tmp_path, llm=ScriptedLLM(),
                           goal_interval_s=0)
        agent.state.set_within_bounds("desires.curiosity_gap", 0.9)
        agent.maybe_proactive()
        pid = agent.pending_proposals()[0]["proposal_id"]
        before_p = agent.state.emotion["pleasure"]
        before_d = agent.state.emotion["dominance"]
        agent.deny_proposal(pid)
        assert agent.state.emotion["pleasure"] < before_p
        assert agent.state.emotion["dominance"] < before_d

    def test_owner_praise_detected_in_chat(self, tmp_path):
        agent = ProtoAgent(data_dir=tmp_path, llm=ScriptedLLM(),
                           goal_interval_s=10 ** 9)
        before = agent.state.emotion["pleasure"]
        agent.chat("干得漂亮，继续保持")
        assert agent.state.emotion["pleasure"] > before

    def test_emotion_decays_each_turn(self, tmp_path):
        agent = ProtoAgent(data_dir=tmp_path, llm=ScriptedLLM(),
                           goal_interval_s=10 ** 9)
        agent.state.emotion["arousal"] = 0.95
        for _ in range(5):
            agent.chat("嗯")
        assert agent.state.emotion["arousal"] < 0.95
