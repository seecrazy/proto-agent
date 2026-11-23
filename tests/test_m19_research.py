# -*- coding: utf-8 -*-
"""M19: 研究驱动的三轮改进（Agent 设计/人类研究/长时可靠性）。"""
import json
import time

import pytest

from proto.llm.client import LLMClient
from proto.runtime.agent import ProtoAgent


class Res(LLMClient):
    """脚本化：归因[]/回复/重要度评分/计划。"""
    def __init__(self):
        self.users = []
    def complete(self, system, user):
        self.users.append(f"{system[:12]}|{user}")
        if "归因" in system:
            return "[]"
        if "重要度" in system:
            return "9"
        if "计划" in system:
            return "先看时间再汇报"
        return "好的。"


class _DeciderLLM(LLMClient):
    def __init__(self, decisions):
        self.decisions = list(decisions)
    def complete(self, system, user):
        if "归因" in system:
            return "[]"
        if self.decisions:
            return json.dumps(self.decisions.pop(0))
        return json.dumps({"done": True, "summary": "ok"})


def dead_web():
    from proto.tools.webtools import WebToolkit
    return WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))


@pytest.fixture()
def agent(tmp_path):
    return ProtoAgent(data_dir=tmp_path, llm=Res(), goal_interval_s=10 ** 9,
                      webtools=dead_web())


class TestRound1AgentDesign:
    def test_llm_importance_scoring_at_write(self, agent):
        """Generative Agents：写入时 LLM 评重要度（1-10）。"""
        reply = agent.chat("主人的公司融资成功了")
        agent.remember_turn("主人的公司融资成功了", reply)
        latest = agent.memory.episodes()[-1]
        assert latest.importance >= 0.85      # LLM 评 9 → 0.9

    def test_reflexion_lessons_injected_into_loop(self, agent):
        """Reflexion：失败教训显式注入后续任务决策。"""
        agent.library.add("当涉及 API 时避免：先改后测", cluster="chat",
                          utility=0.5, valence=-0.1)
        agent.llm.users.clear()
        from proto.exec.agentic import AgenticLoop
        agent.llm_users = agent.llm.users
        AgenticLoop(agent, max_steps=1).run("调 API 写集成")
        joined = "\n".join(str(u) for u in agent.llm.users)
        assert "教训" in joined and "先改后测" in joined

    def test_plan_first_injected(self, agent):
        agent.llm.users.clear()
        from proto.exec.agentic import AgenticLoop
        AgenticLoop(agent, max_steps=1).run("看时间")
        assert any("计划" in str(u) or "先看时间" in str(u) for u in agent.llm.users)


class TestRound2HumanResearch:
    def test_hedonic_adaptation_repeated_praise(self, agent):
        """多巴胺 RPE 习惯化：3h 内重复夸奖，愉悦增量几何衰减。"""
        boosts = []
        for i in range(3):
            agent.state.emotion["pleasure"] = 0.0
            agent.chat("干得好")
            boosts.append(agent.state.emotion["pleasure"])
        assert boosts[0] > boosts[1] > boosts[2]                # 递减
        # 时间恢复：清空历史后回到全量
        agent._praise_times.clear()
        agent.state.emotion["pleasure"] = 0.0
        agent.chat("干得好")
        assert agent.state.emotion["pleasure"] == pytest.approx(boosts[0])

    def test_circadian_arousal_setpoint(self):
        from proto.state.emotion import circadian_arousal_setpoint
        day = circadian_arousal_setpoint(14)    # 下午
        night = circadian_arousal_setpoint(3)   # 凌晨
        assert day > night and 0.2 <= night < day <= 0.6

    def test_zeigarnik_unfinished_remembered(self, agent):
        """真实暂停流：第一步 visible_write → 暂停 → 未完成情景重要度加成。"""
        from proto.exec.agentic import AgenticLoop
        agent_llm = agent.llm
        agent_llm.users.clear()
        agent.llm = _DeciderLLM([{"tool": "write_file",
                                  "args": {"path": "shared.md", "content": "x"},
                                  "category": "visible_write"}])
        try:
            result = AgenticLoop(agent, max_steps=2).run("发布文档")
            assert result["status"] == "paused"
        finally:
            agent.llm = agent_llm
        unfinished = [m for m in agent.memory.episodes() if "未完成" in m.text]
        assert unfinished and unfinished[0].importance >= 0.65   # Zeigarnik 加成

    def test_spaced_reinforcement_extends_half_life(self, tmp_path):
        from proto.memory.stream import MemoryStream
        from proto.storage.store import FileSystemStore
        ms = MemoryStream(FileSystemStore(tmp_path))
        m = ms.add("episode", "重要方法", importance=0.6, ts=100.0)
        now = 100.0 + 10 * 24 * 3600
        base = ms._recency(m, now)
        ms.reinforce(m.id, ts=now - 5 * 24 * 3600)
        ms.reinforce(m.id, ts=now)
        boosted = ms._recency(m, now)
        assert boosted > base            # 提取练习 → 半衰期延长


class TestRound3Reliability:
    def test_llm_circuit_breaker(self, tmp_path):
        from proto.llm.breaker import CircuitBreaker
        cb = CircuitBreaker(fail_threshold=3, cooldown_s=60)
        for _ in range(3):
            cb.record_failure()
        assert not cb.allow()            # 打开：熔断
        cb._opened_at -= 61              # 冷却已过 → 半开
        assert cb.allow()
        cb.record_success()
        assert cb.allow()

    def test_breaker_guarded_in_agent(self, tmp_path):
        class Flaky(LLMClient):
            def __init__(self):
                self.n = 0
            def complete(self, s, u):
                self.n += 1
                raise OSError("网络炸了")
        a = ProtoAgent(data_dir=tmp_path, llm=Flaky(), goal_interval_s=10 ** 9,
                       webtools=dead_web())
        for _ in range(4):
            a.chat("你好")               # 回退离线回复，不崩
        assert not a._breaker.allow()    # 连续失败后熔断
        assert a.llm.n == 3              # 熔断后不再打 LLM（第 4 次直连离线）
        assert a.spine.find(lambda d: d["event_type"] == "llm_breaker_trip")

    def test_study_avoids_recently_read_repos(self, tmp_path):
        from proto.memory.stream import MemoryStream
        from proto.runtime.study import _novel_repos
        from proto.storage.store import FileSystemStore
        repos = [{"full_name": "a/seen", "stars": 9},
                 {"full_name": "b/fresh", "stars": 8}]
        memory = MemoryStream(FileSystemStore(tmp_path))
        memory.add("episode", "研读了 a/seen 的 README", ts=time.time())
        fresh = _novel_repos(repos, memory, days=30)
        assert [r["full_name"] for r in fresh] == ["b/fresh"]


def test_nighttime_heartbeat_throttled():
    from proto.web.server import ProtoWebServer
    assert ProtoWebServer._effective_interval(30, 14) == 30    # 白天正常
    assert ProtoWebServer._effective_interval(30, 3) == 150    # 凌晨 ×5
    assert ProtoWebServer._effective_interval(30, 23) == 150   # 深夜 ×5
