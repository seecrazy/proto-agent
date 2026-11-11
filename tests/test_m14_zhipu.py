# -*- coding: utf-8 -*-
"""M14: 智谱 Coding Plan（Anthropic 兼容）+ 双模型栈 + llmcheck 自检。"""
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest


@pytest.fixture()
def anthropic_server():
    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            received["body"] = json.loads(self.rfile.read(length))
            received["x-api-key"] = self.headers.get("x-api-key")
            received["version"] = self.headers.get("anthropic-version")
            received["path"] = self.path
            data = json.dumps({"content": [{"type": "text", "text": "你好，主人"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/anthropic", received
    server.shutdown()


class TestAnthropicClient:
    def test_messages_protocol(self, anthropic_server):
        from proto.llm.client import AnthropicCompatClient

        base, received = anthropic_server
        client = AnthropicCompatClient(base_url=base, api_key="sk-zp", model="glm-5.3")
        assert client.complete("系统提示", "用户输入") == "你好，主人"
        assert received["path"].endswith("/v1/messages")
        assert received["x-api-key"] == "sk-zp"
        assert received["version"]  # anthropic-version 头
        assert received["body"]["model"] == "glm-5.3"
        assert received["body"]["system"] == "系统提示"
        assert received["body"]["messages"][0]["role"] == "user"

    def test_error_raises_llm_error(self):
        from proto.llm.client import AnthropicCompatClient, LLMError

        client = AnthropicCompatClient(base_url="http://127.0.0.1:9/x",
                                       api_key="k", model="m")
        with pytest.raises(LLMError):
            client.complete("s", "u")


class TestProviderPresets:
    def test_zhipu_coding_plan_preset(self, monkeypatch):
        from proto.llm.client import AnthropicCompatClient, create_llm_stack

        monkeypatch.setenv("PROTO_LLM_PROVIDER", "zhipu-coding")
        monkeypatch.setenv("PROTO_LLM_API_KEY", "key-xxx")
        stack = create_llm_stack()
        assert isinstance(stack["main"], AnthropicCompatClient)
        assert "bigmodel.cn/api/anthropic" in stack["main"].describe()
        assert "glm-5.3" in stack["main"].describe()

    def test_dual_model_stack(self, monkeypatch):
        from proto.llm.client import AnthropicCompatClient, create_llm_stack

        monkeypatch.setenv("PROTO_LLM_PROVIDER", "zhipu-coding")
        monkeypatch.setenv("PROTO_LLM_API_KEY", "k")
        monkeypatch.setenv("PROTO_LLM_FAST_MODEL", "glm-5.3-flash")
        stack = create_llm_stack()
        assert stack["fast"] is not stack["main"]
        assert "glm-5.3-flash" in stack["fast"].describe()
        assert "glm-5.3" in stack["main"].describe() and "flash" not in stack["main"].describe()

    def test_fast_falls_back_to_main(self, monkeypatch):
        from proto.llm.client import create_llm_stack

        monkeypatch.setenv("PROTO_LLM_PROVIDER", "zhipu-coding")
        monkeypatch.setenv("PROTO_LLM_API_KEY", "k")
        monkeypatch.delenv("PROTO_LLM_FAST_MODEL", raising=False)
        stack = create_llm_stack()
        assert stack["fast"] is stack["main"]

    def test_base_url_with_anthropic_suffix_autodetects_style(self, monkeypatch):
        from proto.llm.client import AnthropicCompatClient, create_llm_stack

        monkeypatch.delenv("PROTO_LLM_PROVIDER", raising=False)
        monkeypatch.setenv("PROTO_LLM_BASE_URL", "https://open.bigmodel.cn/api/anthropic")
        monkeypatch.setenv("PROTO_LLM_API_KEY", "k")
        stack = create_llm_stack()
        assert isinstance(stack["main"], AnthropicCompatClient)


class TestAgentFastModelRouting:
    def test_extraction_uses_fast_client(self, tmp_path):
        from proto.exec.webtools import WebToolkit
        from proto.llm.client import LLMClient
        from proto.runtime.agent import ProtoAgent

        class Main(LLMClient):
            def complete(self, s, u):
                return "[]" if "归因" in s else "主模型回复。"

        class Fast(LLMClient):
            def __init__(self):
                self.hits = []
            def complete(self, s, u):
                self.hits.append(s[:10])
                return "[]" if "归因" in s else json.dumps(
                    [{"text": "主人名叫小明", "importance": 0.9}])

        fast = Fast()
        agent = ProtoAgent(data_dir=tmp_path, llm=Main(), fast_llm=fast,
                           goal_interval_s=10 ** 9,
                           webtools=WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网"))))
        agent.chat("我叫小明")
        assert fast.hits                       # 抽取走快模型
        assert any("小明" in m.text for m in agent.memory.facts())
        assert agent.llm is not agent.llm_fast
