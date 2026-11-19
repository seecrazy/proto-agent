# -*- coding: utf-8 -*-
"""M51：活动中心——主人不问就能看到它在干嘛/干了什么/落盘了什么。"""
import json
import threading
import urllib.request

import pytest

from proto.llm.client import LLMClient
from proto.web.server import ProtoWebServer


class ScriptedLLM(LLMClient):
    def complete(self, system, user):
        return "好的。"


@pytest.fixture()
def server(tmp_path):
    from proto.runtime.agent import ProtoAgent
    agent = ProtoAgent(data_dir=tmp_path / "web", llm=ScriptedLLM(),
                       goal_interval_s=0)
    srv = ProtoWebServer(agent, host="127.0.0.1", port=0, heartbeat_s=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def call(srv, method, path):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", method=method)
    req.add_header("X-Proto-Token", srv.token)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


class TestLatestRound:
    def test_parse_latest_round_fields(self, tmp_path):
        from proto.work.journal import GoalJournal
        j = GoalJournal(tmp_path / "J.md")
        j.note_round("awaiting_retry", ["规格已齐"],
                     ["projects/A/SPEC.md"], ["写 App.swift"])
        j.note_round("verified", ["入口完成"], ["projects/A/App.swift"],
                     ["补单测"])
        lr = j.latest_round()
        assert lr["status"] == "verified"
        assert "App.swift" in lr["produced"]
        assert "补单测" in lr["next"]
        assert j.round_count() == 2

    def test_empty_journal(self, tmp_path):
        from proto.work.journal import GoalJournal
        j = GoalJournal(tmp_path / "none" / "J.md")
        assert j.latest_round() is None
        assert j.round_count() == 0


class TestActivityEndpoint:
    def test_marker_journals_files_visible(self, server, tmp_path):
        import time
        agent = server.manager.get(server.manager.current_id())
        # 实时标记
        agent._current_op = {"kind": "chain", "goal": "以 SwiftUI 实现温故 App",
                             "step": 8, "max_steps": 24, "tool": "write_file",
                             "detail": "projects/Wengu/Sources/Models.swift",
                             "since": time.time() - 180}
        # 活跃目标 + 日志
        node = agent.goal_tree.add("以 SwiftUI 实现温故 App", "mid")
        from pathlib import Path
        from proto.work.journal import GoalJournal
        j = GoalJournal(Path(agent.data_dir) / "work" / node.id / "JOURNAL.md")
        j.note_round("max_steps", ["数据模型已定"],
                     ["projects/Wengu/Sources/Models.swift"], ["写 SrsEngine"])
        # 近期落盘文件
        wdir = agent.code_workspace.root / "projects" / "Wengu"
        wdir.mkdir(parents=True, exist_ok=True)
        (wdir / "README.md").write_text("温故", encoding="utf-8")
        a = call(server, "GET", "/api/activity")
        assert a["current"]["step"] == 8 and "温故" in a["current"]["goal"]
        assert a["current"]["elapsed_s"] >= 170
        assert a["goal_work"] and a["goal_work"][0]["rounds"] == 1
        assert "Models.swift" in a["goal_work"][0]["produced"]
        assert any("Wengu/README.md" in f["path"] for f in a["recent_files"])

    def test_archived_excluded_from_recent_files(self, server):
        agent = server.manager.get(server.manager.current_id())
        arch = agent.code_workspace.root / "projects" / "_archived" / "old"
        arch.mkdir(parents=True)
        (arch / "x.md").write_text("x", encoding="utf-8")
        a = call(server, "GET", "/api/activity")
        assert not any("_archived" in f["path"] for f in a["recent_files"])


class TestFrontendWiring:
    def test_activity_card_sources(self):
        from pathlib import Path
        html = (Path(__file__).resolve().parent.parent
                / "proto" / "web" / "index.html").read_text(encoding="utf-8")
        assert 'id="activity-card"' in html
        assert "refreshActivity" in html
        assert "/api/activity" in html
        assert '"activity", refreshActivity' in html
