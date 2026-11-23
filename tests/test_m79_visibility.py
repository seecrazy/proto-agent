# -*- coding: utf-8 -*-
"""M79：主人可见性四件套回归。

① 聊天历史回放（GET /api/chat/history）
② JOURNAL：轮次编号跨进程接续、机械摘要不留死引用、句子边界截断
③ 排队续跑 run 在舰队暂停时报 fleet_paused（可行动卡点），live 快照带 blocked
④ 执行链时间线（GET /api/runs/{id}/timeline）：每步工具/意图 note/结果/失败行，
   调度代次 + 会话连续性（老 24 步续跑 vs 新开）
"""
import json
import threading

import pytest

from proto.llm.client import LLMClient
from proto.runtime.agent import ProtoAgent
from proto.web.server import ProtoWebServer
from proto.work.journal import GoalJournal
from tests.test_web import call


class LoopScript(LLMClient):
    """脚本化决策：第 1 步带 note 的工具调用，第 2 步 done。"""

    def __init__(self):
        self.loop_steps = 0

    def complete(self, system, user):
        if "独立验收" in system:
            return json.dumps({"verdict": "pass"})
        if "最小可检验定义" in system:
            return json.dumps({"acceptance": "clock 返回当前时间"})
        if "任务执行循环" in system:
            self.loop_steps += 1
            if self.loop_steps == 1:
                return json.dumps({"tool": "clock", "args": {}, "category": "read_draft",
                                   "note": "读一次时钟确认工具链可用"})
            return json.dumps({"done": True, "summary": "已读取时间"})
        if "归因" in system:
            return json.dumps([{"param": "instincts.social_initiative",
                                "direction": 1, "strength": 0.6}])
        return "好的主人。"


@pytest.fixture()
def server(tmp_path):
    agent = ProtoAgent(data_dir=tmp_path / "m79", llm=LoopScript(),
                       goal_interval_s=0)
    srv = ProtoWebServer(agent, host="127.0.0.1", port=0, heartbeat_s=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


# ---- ① 聊天历史 ----

class TestChatHistory:
    def test_history_replays_saved_turns(self, server):
        for text in ("你好呀", "今天做了什么"):
            status, _ = call(server, "POST", "/api/chat", {"text": text}, token=server.token)
            assert status == 200
        status, data = call(server, "GET", "/api/chat/history?n=10", token=server.token)
        assert status == 200
        roles = [turn["role"] for turn in data["history"]]
        texts = [turn["text"] for turn in data["history"]]
        assert "owner" in roles and "agent" in roles
        assert "今天做了什么" in texts

    def test_history_requires_auth_and_bounds(self, server):
        assert call(server, "GET", "/api/chat/history")[0] == 401
        assert call(server, "GET", "/api/chat/history?n=0", token=server.token)[0] == 400
        assert call(server, "GET", "/api/chat/history?n=abc", token=server.token)[0] == 400


# ---- ④ 执行链时间线 ----

class TestRunTimeline:
    def test_timeline_shows_steps_note_and_chain(self, server):
        agent = server.manager.get(server.manager.current_id())
        runs = server.manager.runs
        run = runs.enqueue(agent.fleet_id, "task", {"text": "读取时钟"})
        final = runs.wait(run["id"], timeout=8.0)
        assert final["status"] == "succeeded", final
        status, data = call(server, "GET", f"/api/runs/{run['id']}/timeline",
                            token=server.token)
        assert status == 200
        assert data["run"]["kind"] == "task" and data["run"]["session_id"]
        assert data["intent"]["task"].startswith("读取时钟")
        assert data["progress"]["max_steps"] >= 1
        notes = [step["note"] for step in data["steps"]]
        assert any("时钟" in note for note in notes), data["steps"]
        clocks = [step for step in data["steps"] if step["tool"] == "clock"]
        assert clocks and clocks[0]["ok"] is True and clocks[0]["ts"]
        # 首轮调度即新开执行链
        assert data["chain"]["attempt"] == 1
        assert data["chain"]["same_as_previous"] is False
        assert data["outcome"]["summary"]

    def test_timeline_unknown_run_404(self, server):
        status, _ = call(server, "GET", "/api/runs/" + "0" * 32 + "/timeline",
                         token=server.token)
        assert status == 404


# ---- ③ 暂停舰队的排队续跑 ----

class TestPausedContinuation:
    def test_blocked_reason_reports_fleet_paused_for_continuation(self, server):
        agent = server.manager.get(server.manager.current_id())
        runs = server.manager.runs
        run = runs.enqueue(agent.fleet_id, "task", {"text": "读取时钟"})
        assert runs.wait(run["id"], timeout=8.0)["status"] == "succeeded"
        # 先暂停再入队：普通 tick 在暂停舰队下不会被调度，稳定停在 queued；
        # 再把 result 伪装成续跑（resume_session=True），模拟昨晚 429 后
        # 进入 model_retry_wait 的真实形态。
        server.manager.pause(agent.fleet_id, True)
        try:
            queued = runs.enqueue(agent.fleet_id, "tick", {})
            with runs.database.connect(write=True) as connection:
                connection.execute(
                    "UPDATE runs SET result = ?, session_id = ? WHERE id = ?",
                    (json.dumps({"resume_session": True, "status": "waiting_model",
                                 "model_reason": "model_rate_limited"}),
                     run["session_id"], queued["id"]))
            assert runs.get(queued["id"])["status"] == "queued"
            assert runs.blocked_reason(runs.get(queued["id"])) == "fleet_paused"
            status, feed = call(server, "GET", "/api/activity/live", token=server.token)
            assert status == 200
            assert feed["run"]["id"] == queued["id"]
            assert feed["blocked"] == "fleet_paused"
        finally:
            server.manager.pause(agent.fleet_id, False)
        # 解除暂停后回到原本的等待语义
        assert runs.blocked_reason(runs.get(queued["id"])) == ""


# ---- ② JOURNAL ----

class TestJournalContinuity:
    def test_round_numbering_continues_across_instances(self, tmp_path):
        path = tmp_path / "J.md"
        GoalJournal(path).note_round("max_steps", ["a"], ["p/a"], ["b"])
        GoalJournal(path).note_round("max_steps", ["c"], [], ["d"])
        text = path.read_text(encoding="utf-8")
        assert "第1轮" in text and "第2轮" in text and "第3轮" not in text

    def test_mechanical_findings_never_leave_dead_references(self, tmp_path):
        j = GoalJournal(tmp_path / "J.md")
        trace = [
            {"tool": "read_files", "args": {"paths": ["a"]}, "result": "…", "ok": True},
            {"tool": "edit_file", "args": {"path": "projects/x/a.md"},
             "result": "failed: 未找到要替换的文本片段", "ok": False},
            {"tool": "write_file", "args": {"path": "projects/x/b.md"},
             "result": "written:projects/x/b.md", "ok": True},
        ]
        j.finish("awaiting_retry", ["观察"], trace, summarize=None)
        head = j.resume_head()
        assert "observations" not in head          # 不再出现死引用
        assert "失败" in head and "write_file" in head.lower() or "写入" in head or "落盘" in head
        assert "projects/x/b.md" in head

    def test_placeholder_llm_lines_are_dropped(self, tmp_path):
        j = GoalJournal(tmp_path / "J.md")

        def fake(prompt, material):
            return "发现：（待填：本轮摸清的关键事实）\n产出：无\n下一步：检查 X"
        j.finish("max_steps", [], [], summarize=fake)
        head = j.resume_head()
        assert "待填" not in head
        assert "检查 X" in head

    def test_sentence_boundary_cut(self, tmp_path):
        j = GoalJournal(tmp_path / "J.md")
        long = "第一句完整保留。第二句也很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长"
        j.note_round("max_steps", [long], [], [])
        head = j.resume_head()
        assert "第一句完整保留。" in head
        assert head.count("…") <= 1
