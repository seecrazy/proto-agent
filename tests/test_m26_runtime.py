# -*- coding: utf-8 -*-
"""M26: 运行时测试——页面源健康门 + 真实服务 E2E 全流程。

这次事故的教训：api("GET", path, {}) 的空对象使 fetch 带 body 被浏览器拒绝，
8 张卡片自上线起静默挂死，纯后端测试全绿。故加两道门：
① 页面源断言（GET 不带 body / 关键函数存在 / node --check 语法门）；
② 真实服务 HTTP E2E（舰队 CRUD→切换→作用域交互→活动观测）。
"""
import json
import re
import shutil
import subprocess
import threading
import urllib.request

import pytest

HTML = "proto/web/index.html"


class TestPageHealthGate:
    def test_no_get_with_body_anywhere(self):
        html = open(HTML, encoding="utf-8").read()
        offenders = re.findall(r'api\("GET",\s*"[^"]+"\s*,\s*\{', html)
        assert not offenders, f"GET 调用不允许带 body 参数: {offenders}"

    def test_api_helper_strips_get_body(self):
        html = open(HTML, encoding="utf-8").read()
        assert "GET/HEAD 绝不带 body" in html
        assert re.search(r'payload = \(method === "GET"', html)

    def test_key_ui_functions_defined(self):
        html = open(HTML, encoding="utf-8").read()
        for fn in ("refreshFleet", "switchAgent", "toggleActivity", "showCreateAgent",
                   "pauseAgent", "archiveAgent", "refreshGoalTree", "refreshMemory"):
            assert f"function {fn}" in html or f"async function {fn}" in html, fn

    def test_js_syntax_gate(self):
        node = shutil.which("node")
        if not node:
            pytest.skip("无 node（CI 可装）")
        html = open(HTML, encoding="utf-8").read()
        scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
        tmp = "/tmp/proto_ui_check.js"
        open(tmp, "w").write(scripts[-1])
        result = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


class TestConsoleE2E:
    def test_full_console_flow_over_http(self, tmp_path):
        from proto.llm.client import LLMClient
        from proto.runtime.manager import AgentManager
        from proto.web.server import ProtoWebServer
        from proto.tools.webtools import WebToolkit

        class Null(LLMClient):
            def complete(self, s, u):
                return "[]" if "归因" in s else "好的。"

        mgr = AgentManager(root=tmp_path, llm=Null(),
                           webtools=WebToolkit(fetch=lambda u, a="t":
                                               (_ for _ in ()).throw(OSError("禁网"))))
        server = ProtoWebServer(manager=mgr, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.port}"

        def call(method, path, body=None, agent=None):
            req = urllib.request.Request(
                base + path, data=json.dumps(body).encode() if body else None,
                method=method)
            req.add_header("X-Proto-Token", server.token)
            if body:
                req.add_header("Content-Type", "application/json")
            if agent:
                req.add_header("X-Proto-Agent", agent)
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read().decode())

        try:
            # 首页可服务且含舰队 UI
            page = urllib.request.urlopen(base + "/", timeout=5).read().decode()
            assert "agent-select" in page and "toggleActivity" in page
            # 舰队创建 → 切换 → 作用域交互 → 活动观测
            created = call("POST", "/api/agents",
                           {"name": "E2E 专家", "persona": "软件专家"})["created"]
            rows = call("GET", "/api/agents")
            target = next(a for a in rows["agents"] if a["id"] == created["id"])
            assert target["activity"]["status"]
            call("POST", "/api/chat", {"text": "你好"}, agent=created["id"])
            call("POST", f"/api/agents/{created['id']}/pause", {"on": True})
            rows = call("GET", "/api/agents")
            assert next(a for a in rows["agents"]
                        if a["id"] == created["id"])["paused"] is True
            call("POST", f"/api/agents/{created['id']}/pause", {"on": False})
            # 切换后无头路由默认指向新 current
            call("POST", f"/api/agents/{created['id']}/select", {})
            state = call("GET", "/api/state")
            assert state["persona"] == "软件专家"
        finally:
            server.shutdown()
            server.server_close()


def test_all_observability_payloads_carry_time():
    """所有观测面 API 的数据都带时间字段（用户要求：看出什么时候产生的）。"""
    import json as _json
    import threading
    import urllib.request as _ur
    import tempfile
    from proto.llm.client import LLMClient
    from proto.runtime.manager import AgentManager
    from proto.web.server import ProtoWebServer
    from proto.tools.webtools import WebToolkit

    class Null(LLMClient):
        def complete(self, s, u): return "[]" if "归因" in s else "好的。"

    mgr = AgentManager(root=tempfile.mkdtemp(), llm=Null(),
                       webtools=WebToolkit(fetch=lambda u, a="t":
                                           (_ for _ in ()).throw(OSError("禁网"))))
    srv = ProtoWebServer(manager=mgr, port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.port}"

    def call(path):
        req = _ur.Request(base + path)
        req.add_header("X-Proto-Token", srv.token)
        with _ur.urlopen(req, timeout=10) as r:
            return _json.loads(r.read().decode())

    try:
        info = mgr.create(name="T", priority="研究 X")
        g = mgr.get(info["id"])
        g.memory.add("fact", "事实T", ts=1700000000)
        g.world.record_prediction("t", "预测T", deadline_ts=1999999999,
                                  created_at=1700000000)
        g.goal_tree.add("目标T", "long", source="owner")
        aid = info["id"]

        def acall(path):
            req = _ur.Request(base + path)
            req.add_header("X-Proto-Token", srv.token)
            req.add_header("X-Proto-Agent", aid)
            with _ur.urlopen(req, timeout=10) as r:
                return _json.loads(r.read().decode())

        mem = acall("/api/memory")
        assert mem["facts"][0]["ts"] == 1700000000
        world = acall("/api/world")
        assert world["topics"]["t"]["predictions"][0]["created_at"] == 1700000000
        assert world["topics"]["t"]["predictions"][0]["deadline_ts"] == 1999999999
        tree = acall("/api/goaltree")
        assert tree["nodes"][-1]["created_at"] > 0
        agents = call("/api/agents")
        row = next(a for a in agents["agents"] if a["id"] == aid)
        assert "at" in row.get("activity", {})
    finally:
        srv.shutdown()
        srv.server_close()
