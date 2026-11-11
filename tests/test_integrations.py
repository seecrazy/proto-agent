# -*- coding: utf-8 -*-
"""M7: 外部接入——存储工厂(MongoDB)、LangGraph 编排、gVisor 沙盒后端、固定 token。"""
import json
import os
import stat
import sys

import pytest

from proto.evolution.sandbox import Sandbox
from proto.storage.mongo_store import create_store
from proto.storage.store import FileSystemStore


class TestStoreFactory:
    def test_default_is_filesystem(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PROTO_STORE", raising=False)
        store = create_store(tmp_path)
        assert isinstance(store, FileSystemStore)

    def test_env_routes_to_mongo_lazily(self, tmp_path, monkeypatch):
        pymongo = pytest.importorskip("pymongo")  # 主环境未装则跳过；venv 验证
        monkeypatch.setenv("PROTO_STORE", "mongo")
        monkeypatch.setenv("PROTO_MONGO_URI", "mongodb://localhost:1")
        monkeypatch.setenv("PROTO_MONGO_DB", "proto_test")
        from proto.storage.mongo_store import MongoStore
        store = create_store(tmp_path)
        assert isinstance(store, MongoStore)  # 构造不触网（懒连接）


class TestLangGraphAdapter:
    def test_turn_graph_full_pipeline(self, tmp_path):
        pytest.importorskip("langgraph")
        from proto.adapters.langgraph_runner import build_turn_graph
        from proto.llm.client import LLMClient
        from proto.runtime.agent import ProtoAgent

        class ScriptedLLM(LLMClient):
            def __init__(self):
                self.n = 0
            def complete(self, system, user):
                self.n += 1
                return (json.dumps([{"param": "instincts.social_initiative",
                                     "direction": 1, "strength": 0.6}])
                        if self.n == 1 else "好的主人。")

        agent = ProtoAgent(data_dir=tmp_path, llm=ScriptedLLM(), goal_interval_s=10**9)
        app = build_turn_graph(agent)
        result = app.invoke({"text": "以后更主动一点"})
        assert result["reply"] == "好的主人。"
        # 归因→调制在图节点里真实发生
        assert agent.state.get("instincts.social_initiative") == pytest.approx(0.52)


class TestSandboxBackends:
    CODE = "def solve(items):\n    return '|'.join(items)\n"

    def test_auto_on_darwin_is_subprocess(self):
        if sys.platform != "darwin":
            pytest.skip("macOS 路径")
        assert Sandbox(backend="auto").backend == "subprocess"

    def test_runsc_backend_uses_runsc_command(self, tmp_path):
        """用假 runsc（记录 argv 并照常执行）验证命令构造，平台无关。"""
        marker = tmp_path / "runsc_argv.txt"
        fake = tmp_path / "runsc"
        fake.write_text(
            "#!/bin/sh\n"
            f'echo "$@" > "{marker}"\n'
            'shift\n'  # 去掉 "do"
            'exec "$@"\n')
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        sandbox = Sandbox(backend="runsc", runsc_path=str(fake), timeout_s=15)
        result = sandbox.run_variant(self.CODE,
                                     [{"input": ["a"], "expected": "a"}])
        assert result.passed == 1
        argv = marker.read_text().split("\n")[0]
        assert argv.startswith("do ")
        assert "runner.py" in argv

    def test_runsc_without_binary_rejected(self):
        with pytest.raises(ValueError):
            Sandbox(backend="runsc", runsc_path=None)


class TestWebFixedToken:
    def test_injected_token_used(self, tmp_path):
        from proto.llm.client import LLMClient
        from proto.runtime.agent import ProtoAgent
        from proto.web.server import ProtoWebServer

        class Null(LLMClient):
            def complete(self, s, u):
                return "[]"

        agent = ProtoAgent(data_dir=tmp_path, llm=Null())
        server = ProtoWebServer(agent, port=0, token="my-fixed-token")
        assert server.token == "my-fixed-token"
        server.server_close()
