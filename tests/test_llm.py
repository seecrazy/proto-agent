# -*- coding: utf-8 -*-
"""M5-a: OpenAI 兼容 LLM 客户端（urllib 零依赖）+ LLMAttributor + LLMPlanner。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from proto.kernel.anchor import Channel
from proto.llm.client import LLMClient, OpenAICompatClient
from proto.exec.router import ToolRegistry
from proto.metacog.attribution import HeuristicAttributor, LLMAttributor
from proto.exec.planner import LLMPlanner


class FakeClient(LLMClient):
    def __init__(self, reply: str):
        self.reply = reply
        self.calls = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append({"system": system, "user": user})
        return self.reply


@pytest.fixture()
def http_llm():
    """起一个本地 HTTP 服务模拟 /chat/completions。"""
    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received["body"] = json.loads(self.rfile.read(length))
            received["auth"] = self.headers.get("Authorization")
            payload = {"choices": [{"message": {"role": "assistant", "content": "你好，主人"}}]}
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v1", received
    server.shutdown()


class TestOpenAICompatClient:
    def test_complete_parses_response(self, http_llm):
        base, received = http_llm
        client = OpenAICompatClient(base_url=base, api_key="sk-test", model="test-model")
        assert client.complete("系统提示", "用户输入") == "你好，主人"
        assert received["body"]["model"] == "test-model"
        assert received["auth"] == "Bearer sk-test"
        assert received["body"]["messages"][0]["role"] == "system"

    def test_http_error_raises_llm_error(self):
        import socket
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
        sock.close()  # 端口确定无人监听 → 连接拒绝
        from proto.llm.client import LLMError
        client = OpenAICompatClient(base_url=f"http://127.0.0.1:{dead_port}/v1", model="m")
        with pytest.raises(LLMError):
            client.complete("s", "u")


class TestLLMAttributor:
    def test_parses_json_attribution(self):
        fake = FakeClient(json.dumps([{"param": "instincts.social_initiative",
                                       "direction": 1, "strength": 0.6}]))
        attr = LLMAttributor(fake)
        sigs = attr.attribute("以后更主动一点", Channel.OWNER)
        assert len(sigs) == 1
        assert sigs[0].param == "instincts.social_initiative"
        assert sigs[0].strength == pytest.approx(0.6)
        assert sigs[0].channel is Channel.OWNER

    def test_json_in_code_fence_parsed(self):
        fake = FakeClient("```json\n[{\"param\": \"personality.rigor\", \"direction\": 1, \"strength\": 0.5}]\n```")
        sigs = LLMAttributor(fake).attribute("更严谨", Channel.SESSION)
        assert sigs and sigs[0].param == "personality.rigor"

    def test_bad_json_falls_back_to_heuristic(self):
        fake = FakeClient("抱歉我不明白")
        attr = LLMAttributor(fake, fallback=HeuristicAttributor())
        sigs = attr.attribute("以后更主动一点", Channel.OWNER)
        assert any(s.param == "instincts.social_initiative" for s in sigs)

    def test_unknown_param_dropped_and_strength_clamped(self):
        fake = FakeClient(json.dumps([
            {"param": "instincts.does_not_exist", "direction": 1, "strength": 1.0},
            {"param": "instincts.exploration", "direction": 1, "strength": 9.0},
        ]))
        sigs = LLMAttributor(fake).attribute("x", Channel.OWNER)
        assert [s.param for s in sigs] == ["instincts.exploration"]
        assert sigs[0].strength == pytest.approx(1.0)  # 输入域在归因边界夹紧


class TestLLMPlanner:
    def _tools(self):
        tools = ToolRegistry()
        tools.register("clock", lambda: "12:00")
        return tools

    def test_valid_steps_validated(self):
        fake = FakeClient(json.dumps([
            {"tool": "clock", "args": {}, "category": "read_draft"},
            {"tool": "hack_mainframe", "args": {}, "category": "read_draft"},
        ]))
        steps = LLMPlanner(fake, self._tools()).plan("看看时间")
        assert steps == [{"tool": "clock", "args": {}, "category": "read_draft"}]

    def test_bad_category_normalized_to_read_draft(self):
        fake = FakeClient(json.dumps([{"tool": "clock", "args": {}, "category": "宇宙级"}]))
        steps = LLMPlanner(fake, self._tools()).plan("x")
        assert steps[0]["category"] == "read_draft"

    def test_bad_json_returns_empty(self):
        fake = FakeClient("无法规划")
        assert LLMPlanner(fake, self._tools()).plan("x") == []
