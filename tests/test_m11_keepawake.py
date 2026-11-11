# -*- coding: utf-8 -*-
"""M11: 防休眠——caffeinate 生命周期 + 主人开关语义。"""
import sys

import pytest

from proto.system.keepawake import KeepAwake


class TestKeepAwake:
    def test_unavailable_platform_reports_false(self):
        ka = KeepAwake(binary_provider=lambda: None)
        assert not ka.available
        assert ka.start() is False
        assert not ka.is_active

    def test_start_stop_lifecycle(self):
        pytest.importorskip("shutil")
        import shutil
        if not shutil.which("caffeinate"):
            pytest.skip("非 macOS 或无 caffeinate")
        ka = KeepAwake()
        assert ka.start() is True
        assert ka.is_active
        ka.stop()
        assert not ka.is_active

    def test_start_is_idempotent(self):
        import subprocess
        from unittest.mock import MagicMock
        fake_proc = MagicMock()
        fake_proc.poll.return_value = None
        ka = KeepAwake(binary_provider=lambda: "/usr/bin/caffeinate")
        ka._proc = fake_proc
        assert ka.start() is True          # 已激活 → 原样返回
        assert ka._proc is fake_proc       # 没有重复 spawn


class TestServerAwakeToggle:
    def test_awake_endpoint_uses_injected_keeper(self, tmp_path):
        import threading

        from proto.llm.client import LLMClient
        from proto.runtime.agent import ProtoAgent
        from proto.web.server import ProtoWebServer

        class Null(LLMClient):
            def complete(self, s, u):
                return "[]"

        class FakeKeeper:
            available = True
            is_active = False
            def start(self):
                self.is_active = True
                return True
            def stop(self):
                self.is_active = False

        keeper = FakeKeeper()
        agent = ProtoAgent(data_dir=tmp_path, llm=Null(), goal_interval_s=10 ** 9)
        server = ProtoWebServer(agent, port=0, keep_awake=keeper)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            import json as _json
            import urllib.request

            def call(method, path, body=None):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{server.port}{path}",
                    data=_json.dumps(body).encode() if body else None, method=method)
                req.add_header("X-Proto-Token", server.token)
                if body:
                    req.add_header("Content-Type", "application/json")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return resp.status, _json.loads(resp.read().decode())

            status, data = call("POST", "/api/awake", {"on": True})
            assert status == 200 and data["awake"] is True and keeper.is_active
            status, data = call("POST", "/api/awake", {"on": False})
            assert data["awake"] is False and not keeper.is_active
            status, data = call("GET", "/api/state")
            assert data["awake_available"] is True
        finally:
            server.shutdown()
            server.server_close()
