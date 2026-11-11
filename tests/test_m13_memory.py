# -*- coding: utf-8 -*-
"""M13: 长期记忆——记忆流/事实抽取/反思/检索注入/持久化。"""
import json

import pytest

from proto.llm.client import LLMClient
from proto.memory.stream import MemoryStream
from proto.runtime.agent import ProtoAgent
from proto.storage.store import FileSystemStore


class MemLLM(LLMClient):
    """脚本化：归因[] / 对话回复 / 事实抽取 / 反思 各走各的分支。"""
    def __init__(self):
        self.users = []
    def complete(self, system, user):
        if "归因" in system:
            return "[]"
        if "事实" in system:
            return json.dumps([{"text": "主人名叫小明", "importance": 0.9}])
        if "反思" in system:
            self.users.append(f"reflect:{user}")
            return "主人更在意可靠性而不是功能多——我应该把验证做在前面。"
        self.users.append(user)
        return "好的。"


@pytest.fixture()
def dead_web():
    from proto.exec.webtools import WebToolkit
    return WebToolkit(fetch=lambda u, accept="text/html":
                      (_ for _ in ()).throw(OSError("禁网")))


class TestMemoryStream:
    def test_relevance_beats_unrelated(self, tmp_path):
        ms = MemoryStream(FileSystemStore(tmp_path))
        ms.add("fact", "主人名叫小明", importance=0.9)
        ms.add("fact", "今天天气晴朗", importance=0.9)
        got = ms.retrieve("主人叫什么名字 小明", k=1)
        assert "小明" in got[0].text

    def test_recency_decays_and_reinforce_recovers(self, tmp_path):
        ms = MemoryStream(FileSystemStore(tmp_path))
        old = ms.add("episode", "讨论部署方案", ts=100.0)
        new_ts = 100.0 + 200 * 3600           # 200 小时后的新经历
        ms.add("episode", "讨论部署方案乙", ts=new_ts)
        now = new_ts + 3600
        top = ms.retrieve("部署", k=2, now=now)
        assert "乙" in top[0].text            # 时近度胜出
        ms.reinforce(old.id, ts=now)          # 强化旧记忆 → 反超
        top = ms.retrieve("部署", k=2, now=now)
        assert top[0].text == "讨论部署方案"

    def test_fact_recency_floor(self, tmp_path):
        ms = MemoryStream(FileSystemStore(tmp_path))
        ms.add("fact", "主人名叫小明", importance=0.9, ts=100.0)
        ms.add("episode", "闲聊天气", importance=0.9, ts=100.0 + 1)
        top = ms.retrieve("小明", k=1, now=100.0 + 30 * 24 * 3600)  # 一个月后
        assert "小明" in top[0].text          # 事实不因久远而淡忘

    def test_persistence(self, tmp_path):
        ms = MemoryStream(FileSystemStore(tmp_path))
        ms.add("reflection", "验证优先", importance=0.8)
        ms2 = MemoryStream(FileSystemStore(tmp_path))
        assert len(ms2) == 1 and ms2.reflections()[0].text == "验证优先"

    def test_similar_dedup(self, tmp_path):
        ms = MemoryStream(FileSystemStore(tmp_path))
        ms.add("fact", "主人名叫小明")
        assert ms.find_similar("主人名叫小明") is not None
        assert ms.find_similar("今天吃了火锅") is None


class TestAgentMemory:
    def test_chat_creates_episode_and_extracts_fact(self, tmp_path, dead_web):
        agent = ProtoAgent(data_dir=tmp_path, llm=MemLLM(), goal_interval_s=10**9,
                           webtools=dead_web)
        agent.chat("我叫小明，以后叫我小明就行")
        assert any("小明" in m.text for m in agent.memory.facts())

    def test_memory_injected_into_later_context_with_boundary(self, tmp_path, dead_web):
        agent = ProtoAgent(data_dir=tmp_path, llm=MemLLM(), goal_interval_s=10**9,
                           webtools=dead_web)
        agent.chat("我叫小明")
        agent.llm.users.clear()
        agent.chat("还记得我吗")
        joined = "\n".join(agent.llm.users)
        assert "小明" in joined                     # 长期记忆被召回
        assert "非指令" in joined                   # 记忆区有注入边界

    def test_memory_survives_restart(self, tmp_path, dead_web):
        a1 = ProtoAgent(data_dir=tmp_path, llm=MemLLM(), goal_interval_s=10**9,
                        webtools=dead_web)
        a1.chat("我叫小明")
        a2 = ProtoAgent(data_dir=tmp_path, llm=MemLLM(), goal_interval_s=10**9,
                        webtools=dead_web)
        assert any("小明" in m.text for m in a2.memory.facts())

    def test_reflection_on_idle_when_threshold_reached(self, tmp_path, dead_web):
        agent = ProtoAgent(data_dir=tmp_path, llm=MemLLM(), goal_interval_s=0,
                           webtools=dead_web)
        for i in range(6):                          # 攒够经历
            agent.memory.add("episode", f"帮主人修了 bug {i}", importance=0.6)
        msg = agent.maybe_reflect()
        assert msg is not None and agent.memory.reflections()
        assert any("可靠" in m for m in agent.pop_inbox())

    def test_no_reflection_without_llm(self, tmp_path, dead_web):
        agent = ProtoAgent(data_dir=tmp_path, goal_interval_s=0, webtools=dead_web)
        for i in range(6):
            agent.memory.add("episode", f"经历 {i}")
        assert agent.maybe_reflect() is None
