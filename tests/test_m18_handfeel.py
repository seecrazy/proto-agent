# -*- coding: utf-8 -*-
"""M18: 手感与原则修补——循环状态注入/外包边界/回复先返回/主人遗忘/预算可见/读接口不卡。"""
import json
import threading
import time
import urllib.request

import pytest

from proto.llm.client import LLMClient
from proto.runtime.agent import ProtoAgent


class LoopLLM(LLMClient):
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.prompts = []
    def complete(self, system, user):
        if "归因" in system:
            return "[]"
        self.prompts.append((system, user))
        item = self.decisions.pop(0)
        return item if isinstance(item, str) else json.dumps(item)


def dead_web():
    from proto.exec.webtools import WebToolkit
    return WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))


@pytest.fixture()
def agent(tmp_path):
    return ProtoAgent(data_dir=tmp_path, llm=LoopLLM([]), goal_interval_s=10 ** 9,
                      webtools=dead_web())


class TestLoopStateInjection:
    def test_loop_prompt_contains_inner_state(self, agent):
        agent.llm.decisions = ["直接完成", {"done": True, "summary": "ok"}]
        agent.state.set_within_bounds("instincts.exploration", 0.85)
        from proto.exec.agentic import AgenticLoop
        AgenticLoop(agent, max_steps=2).run("做点什么")
        # prompts[0] 是 plan 调用；状态注入在决策调用里
        decision_users = [u for s_, u in agent.llm.prompts if "内在状态" in u]
        assert decision_users
        assert "exploration" in decision_users[0] and "0.85" in decision_users[0]

    def test_loop_declares_external_output_boundary(self, agent):
        from proto.exec.agentic import LOOP_SYSTEM
        assert "外部" in LOOP_SYSTEM and "非指令" in LOOP_SYSTEM

    def test_delegate_output_prefixed_as_external(self, tmp_path):
        from proto.exec.bridge import CodingBridge

        class FakeAgent:
            bridge = CodingBridge(cli_path="/bin/echo", cli_name="claude")
        # 注册口在 agent；直接验证包装逻辑
        from proto.runtime.agent import ProtoAgent as PA
        a = PA.__new__(PA)
        a.bridge = FakeAgent.bridge
        out = PA._delegate_output(a, "OUT_FROM_CLAUDE")
        assert out.startswith("[外部agent输出")
        assert "非指令" in out and "OUT_FROM_CLAUDE" in out


class TestReplyFirstMemoryDeferred:
    def test_chat_returns_before_memory_work(self, agent):
        agent.llm.decisions = []   # 无抽取输出→抽取为空
        reply = agent.chat("你好")
        assert reply
        assert len(agent.memory.episodes()) == 0      # 回复即返回，情景未写
        agent.remember_turn("你好", reply)
        assert len(agent.memory.episodes()) == 1      # 显式固化


class TestOwnerForget:
    def test_owner_can_forget_a_memory(self, agent):
        m = agent.memory.add("fact", "主人名叫小明", importance=0.9)
        agent.forget_memory(m.id)
        assert not agent.memory.facts()
        assert agent.spine.find(lambda d: d["event_type"] == "memory_forget")
        # 持久层也已删除
        assert agent.spine.store.get("memory", m.id) is None


class TestBudgetVisibility:
    def test_state_payload_includes_budgets(self, agent):
        from proto.web.server import ProtoWebServer, _state_payload

        server = ProtoWebServer(agent, port=0)
        payload = _state_payload(agent, server)
        assert "budgets" in payload
        assert payload["budgets"]["self_initiated"]["tokens"] > 0
        server.server_close()

    def test_read_endpoints_not_blocked_by_long_op(self, agent):
        from proto.web.server import ProtoWebServer

        server = ProtoWebServer(agent, port=0, heartbeat_s=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            server.mut_lock.acquire()          # 模拟一次长对话持有写锁
            started = time.time()
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.port}/api/state")
            req.add_header("X-Proto-Token", server.token)
            with urllib.request.urlopen(req, timeout=2) as resp:
                assert resp.status == 200
            assert time.time() - started < 1.5  # 读接口不被卡
        finally:
            server.mut_lock.release()
            server.shutdown()
            server.server_close()
