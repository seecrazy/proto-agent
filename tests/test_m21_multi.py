# -*- coding: utf-8 -*-
"""M21: 多自主 Agent——创建/管理/观测/隔离。"""
import json
import threading
import time
import urllib.request

import pytest

from proto.llm.client import LLMClient
from proto.runtime.agent import ProtoAgent
from proto.runtime.manager import AgentManager


class Null(LLMClient):
    def complete(self, s, u):
        return "[]" if "归因" in s else "好的。"


def dead_web():
    from proto.tools.webtools import WebToolkit
    return WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))


@pytest.fixture()
def manager(tmp_path):
    return AgentManager(root=tmp_path / "fleet", llm=Null(),
                        webtools=dead_web())


class TestManager:
    def test_create_and_list(self, manager):
        a = manager.create(name="研究员", persona="资深软件专家",
                           priority="AI 产业")
        b = manager.create(name="管家", persona="生活助理")
        ids = [x["id"] for x in manager.list()]
        assert a["id"] in ids and b["id"] in ids
        assert manager.get(a["id"]).persona == "资深软件专家"

    def test_agents_isolated(self, manager):
        a = manager.create(name="A", persona="a")
        b = manager.create(name="B", persona="b")
        ga, gb = manager.get(a["id"]), manager.get(b["id"])
        assert ga.fingerprint != gb.fingerprint            # 各自锚定
        reply = ga.chat("我叫小明")
        ga.remember_turn("我叫小明", reply)
        assert any("小明" in m.text for m in ga.memory.facts()) or \
            len(ga.memory.episodes()) >= 1
        assert len(gb.memory.episodes()) == 0              # 记忆不串

    def test_tick_all_drives_every_agent(self, manager):
        infos = [manager.create(name=f"S{i}") for i in range(2)]
        for info in infos:
            g = manager.get(info["id"])
            g.configure_autonomy(enabled=True, mode="auto")
            g.scheduler._last_goal -= 10 ** 9              # 强制到点
            g.state.set_within_bounds("desires.curiosity_gap", 0.9)
        manager.tick_all()
        for info in infos:
            assert manager.get(info["id"]).pending_proposals()

    def test_pause_skips_tick(self, manager):
        info = manager.create(name="P")
        g = manager.get(info["id"])
        g.configure_autonomy(enabled=True, mode="auto")
        g.scheduler._last_goal -= 10 ** 9
        g.state.set_within_bounds("desires.curiosity_gap", 0.9)
        manager.pause(info["id"])
        manager.tick_all()
        assert not g.pending_proposals()

    def test_archive_moves_and_updates_current(self, manager):
        a = manager.create(name="first")
        b = manager.create(name="second")
        manager.set_current(b["id"])
        manager.archive(a["id"])
        names = [x["name"] for x in manager.list()]
        assert "first" not in names and "second" in names
        assert manager.current_id() == b["id"]
        assert not (manager.root / "agents" / a["id"]).exists()
        assert any(manager.root.joinpath("archive").glob(f"{a['id']}*"))

    def test_snapshot_fields_for_observation(self, manager):
        info = manager.create(name="O", persona="p")
        rows = manager.list()
        row = next(r for r in rows if r["id"] == info["id"])
        for key in ("name", "persona", "emotion", "desires", "memory_total",
                    "pending_proposals", "goals_progress", "paused"):
            assert key in row


class TestServerMulti:
    def _server(self, manager):
        from proto.web.server import ProtoWebServer
        server = ProtoWebServer(manager=manager, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def _call(self, server, method, path, body=None, agent_id=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.port}{path}",
            data=json.dumps(body).encode() if body else None, method=method)
        req.add_header("X-Proto-Token", server.token)
        if body:
            req.add_header("Content-Type", "application/json")
        if agent_id:
            req.add_header("X-Proto-Agent", agent_id)
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())

    def test_crud_and_scoped_chat(self, manager):
        server = self._server(manager)
        try:
            created = self._call(server, "POST", "/api/agents",
                                 {"name": "研究员", "persona": "软件专家",
                                  "priority": "AI"})
            agents = self._call(server, "GET", "/api/agents")
            assert any(a["name"] == "研究员" for a in agents["agents"])
            other = self._call(server, "POST", "/api/agents", {"name": "B"})["created"]
            # 头部作用域：对 B 说话，记忆落在 B
            self._call(server, "POST", "/api/chat", {"text": "你好"},
                       agent_id=other["id"])
            # chat 先回包后固化（零等待设计）：有界轮询等后置记忆落库
            gb = manager.get(other["id"])
            for _ in range(50):
                if gb.memory.snapshot()["episodes"] == 1:
                    break
                time.sleep(0.02)
            assert gb.memory.snapshot()["episodes"] == 1   # 记忆落在 B（服务端已后置固化）
            ga = manager.get(created["created"]["id"])
            assert ga.memory.snapshot()["episodes"] == 0   # A 不受影响（隔离）
            # 切换与暂停
            self._call(server, "POST", f"/api/agents/{other['id']}/pause",
                       {"on": True})
            assert any(r["paused"] for r in
                       self._call(server, "GET", "/api/agents")["agents"])
        finally:
            server.shutdown()
            server.server_close()

    def test_single_agent_backcompat(self, tmp_path):
        """旧的单 agent 构造方式仍然工作（自动收编为舰队一员）。"""
        from proto.web.server import ProtoWebServer
        agent = ProtoAgent(data_dir=tmp_path / "legacy", llm=Null(),
                           goal_interval_s=10 ** 9, webtools=dead_web())
        server = ProtoWebServer(agent=agent, port=0)
        assert server.manager.list()[0]["name"] == "默认"
        server.server_close()
