# -*- coding: utf-8 -*-
"""M17: 内在机制按设计落地——欲望满足动力学/情绪标记/重要度半衰期/心境一致检索。"""
import pytest

from proto.llm.client import LLMClient
from proto.runtime.agent import ProtoAgent


class NullLLM(LLMClient):
    def complete(self, system, user):
        return "[]" if "归因" in system else "好的。"


def dead_web():
    from proto.exec.webtools import WebToolkit
    return WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))


@pytest.fixture()
def agent(tmp_path):
    return ProtoAgent(data_dir=tmp_path, llm=NullLLM(), goal_interval_s=10 ** 9,
                      webtools=dead_web())


class TestDesireSatisfaction:
    def test_satisfy_reduces_gap_bounded(self, agent):
        agent.state.set_within_bounds("desires.curiosity_gap", 0.9)
        agent.desires.satisfy("curiosity_gap", 0.3)
        assert agent.state.get("desires.curiosity_gap") == pytest.approx(0.6)
        agent.desires.satisfy("curiosity_gap", 5.0)   # 过量满足也不会为负
        assert agent.state.get("desires.curiosity_gap") == pytest.approx(0.0)

    def test_chat_satisfies_social_connection(self, agent):
        agent.state.set_within_bounds("desires.social_connection_gap", 0.8)
        agent.chat("你好")
        assert agent.state.get("desires.social_connection_gap") == pytest.approx(0.65)

    def test_praise_satisfies_recognition(self, agent):
        agent.state.set_within_bounds("desires.recognition_need", 0.8)
        agent.chat("干得好")
        assert agent.state.get("desires.recognition_need") == pytest.approx(0.5)

    def test_task_completion_satisfies_goal_urgency(self, agent):
        agent.state.set_within_bounds("desires.goal_urgency", 0.8)
        agent.scheduler.on_task_completed(
            type("R", (), {"status": "completed", "retries": 0, "reason": ""})())
        assert agent.state.get("desires.goal_urgency") == pytest.approx(0.55)


class TestEmotionalEncoding:
    def test_episode_records_emotion_snapshot(self, agent):
        agent.state.emotion["arousal"] = 0.9
        agent.chat("重要的事情")
        latest = agent.memory.episodes()[-1]
        assert latest.emotion["arousal"] == pytest.approx(0.9)
        assert latest.importance >= 0.55   # 高唤醒上调编码强度（闪光灯）


class TestImportanceScaledForgetting:
    def test_important_memories_decay_slower(self, tmp_path):
        from proto.memory.stream import MemoryStream
        from proto.storage.store import FileSystemStore

        ms = MemoryStream(FileSystemStore(tmp_path))
        trivia = ms.add("episode", "闲聊天气", importance=0.1, ts=100.0)
        critical = ms.add("episode", "主人项目上线", importance=0.95, ts=100.0)
        now = 100.0 + 30 * 24 * 3600   # 一个月后
        # 重要记忆时近度显著高于琐事（半衰期 72h×(0.5+imp)）
        assert ms._recency(critical, now) > ms._recency(trivia, now) * 5
        # 重要记忆仍可被检索到
        assert any("上线" in m.text for m in ms.retrieve("主人 项目", k=2, now=now))


class TestMoodCongruentRecall:
    def test_positive_mood_boosts_positive_memories(self, tmp_path):
        from proto.memory.stream import MemoryStream
        from proto.storage.store import FileSystemStore

        ms = MemoryStream(FileSystemStore(tmp_path))
        praised = ms.add("episode", "主人夸我做得漂亮", importance=0.7, ts=100.0,
                         emotion={"pleasure": 0.6})
        rejected = ms.add("episode", "提案被主人拒绝", importance=0.7, ts=100.0,
                          emotion={"pleasure": -0.6})
        agent_mood = {"pleasure": 0.8}
        hits = ms.retrieve("主人 我", k=2, now=100.0 + 3600, mood=agent_mood)
        assert hits[0].id == praised.id          # 心情好→先想起被夸
        hits_low = ms.retrieve("主人 我", k=2, now=100.0 + 3600,
                               mood={"pleasure": -0.8})
        assert hits_low[0].id == rejected.id     # 低落→先想起被拒


class TestPostDeployAccess:
    def test_plan_post_deploy(self):
        from proto.setup import plan_post_deploy

        # 装了服务 → 直接给服务 URL（浏览器即开即用）
        assert plan_post_deploy(installed_service=True, launch="web", yes=False)[0] == "open_url"
        # 交互模式默认 → 引导选择
        assert plan_post_deploy(installed_service=False, launch="web", yes=False)[0] == "ask"
        # 非交互/显式 none → 只打印
        assert plan_post_deploy(installed_service=False, launch="none", yes=True)[0] == "print"
        assert plan_post_deploy(installed_service=False, launch="term", yes=True)[0] == "term"
