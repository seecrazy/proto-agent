# -*- coding: utf-8 -*-
"""M6: Web 控制台——JSON API（token 鉴权）+ 页面服务。"""
import json
import threading
import urllib.request

import pytest

from proto.llm.client import LLMClient
from proto.web.server import ProtoWebServer


class ScriptedLLM(LLMClient):
    def __init__(self):
        self.calls = []

    def complete(self, system, user):
        self.calls.append(user)
        if "归因" in system:
            return json.dumps([{"param": "instincts.social_initiative",
                                "direction": 1, "strength": 0.6}])
        return "好的主人。"


@pytest.fixture()
def server(tmp_path):
    from proto.runtime.agent import ProtoAgent
    agent = ProtoAgent(data_dir=tmp_path / "web", llm=ScriptedLLM(),
                       goal_interval_s=0)
    srv = ProtoWebServer(agent, host="127.0.0.1", port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def call(srv, method, path, body=None, token=None):
    url = f"http://127.0.0.1:{srv.port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Proto-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


class TestAuth:
    def test_api_without_token_401(self, server):
        status, _ = call(server, "GET", "/api/state")
        assert status == 401

    def test_api_with_wrong_token_401(self, server):
        status, _ = call(server, "GET", "/api/state", token="bad")
        assert status == 401

    def test_index_served_without_token(self, server):
        with urllib.request.urlopen(f"http://127.0.0.1:{server.port}/", timeout=5) as r:
            html = r.read().decode()
        assert "PROTO" in html and "内在状态" in html


class TestStateAndChat:
    def test_state_has_params_with_bounds(self, server):
        status, data = call(server, "GET", "/api/state", token=server.token)
        assert status == 200
        instincts = data["params"]["instincts"]
        exploration = next(p for p in instincts if p["name"] == "exploration")
        assert exploration["bounds"] == [0.1, 0.9]
        assert "emotion" in data and "model" in data

    def test_chat_modulates_and_replies(self, server):
        status, data = call(server, "POST", "/api/chat",
                            {"text": "以后更主动一点"}, token=server.token)
        assert status == 200 and data["reply"] == "好的主人。"
        assert server.agent.state.get("instincts.social_initiative") == pytest.approx(0.52)

    def test_audit_with_chain_status(self, server):
        call(server, "POST", "/api/chat", {"text": "hi"}, token=server.token)
        status, data = call(server, "GET", "/api/audit?n=5", token=server.token)
        assert status == 200 and data["chain_ok"] and data["events"]


class TestProposalsAndControls:
    def _make_proposal(self, server):
        server.agent.state.set_within_bounds("desires.curiosity_gap", 0.9)
        call(server, "POST", "/api/tick", {}, token=server.token)
        status, data = call(server, "GET", "/api/proposals", token=server.token)
        assert data["proposals"]
        return data["proposals"][0]["proposal_id"]

    def test_deny_proposal_dampens(self, server):
        pid = self._make_proposal(server)
        status, data = call(server, "POST", f"/api/proposals/{pid}/deny",
                            {}, token=server.token)
        assert status == 200
        assert data["threshold"] == pytest.approx(0.65)

    def test_accept_proposal_completes(self, server):
        pid = self._make_proposal(server)
        status, data = call(server, "POST", f"/api/proposals/{pid}/accept",
                            {}, token=server.token)
        assert status == 200 and data["status"] == "completed"

    def test_mute_toggle(self, server):
        status, data = call(server, "POST", "/api/mute", {"on": True}, token=server.token)
        assert status == 200 and data["muted"] is True

    def test_lock_and_override(self, server):
        status, data = call(server, "POST", "/api/lock",
                            {"param": "instincts.exploration", "on": True},
                            token=server.token)
        assert status == 200 and data["locked"] is True
        status, data = call(server, "POST", "/api/override",
                            {"param": "personality.rigor", "value": 0.9},
                            token=server.token)
        assert status == 200 and data["value"] == 0.9

    def test_set_owner_priority(self, server):
        status, data = call(server, "POST", "/api/priority",
                            {"text": "补齐单元测试"}, token=server.token)
        assert status == 200
        assert server.agent.state.owner_priority == "补齐单元测试"

    def test_task_endpoint(self, server):
        status, data = call(server, "POST", "/api/task",
                            {"text": "看一下时间"}, token=server.token)
        assert status == 200
        assert data["status"] in ("completed", "no_plan", "paused")

    def test_evolution_snapshot(self, server):
        status, data = call(server, "GET", "/api/evolution", token=server.token)
        assert status == 200
        assert "variants" in data and "testset_count" in data
