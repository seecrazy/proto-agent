# -*- coding: utf-8 -*-
"""M80 监督现场修复：JOURNAL 模板回显/受阻刷屏/活动细节序列化/live 路由 500。

生产实证（2026-11-23 监督 iPhone 专家时发现）：
1. LLM 把 _SUMMARY_PROMPT 模板原样回显成"发现：（本轮摸清的关键事实）"，
   旧占位符名单只挡"待填"，整块模板文字进了日志；
2. note_model_block 每拍心跳追加一条同文块，单目标刷出 1800+ 条；
3. 活动细节对 dict payload 直接 str() 显示 "{}"；
4. /api/activity/live 对"未暂停的排队续跑 run"500——blocked_reason 收到的
   快照投影没有 result 列，_continuation 读 run["result"] 直接 KeyError
   （M79 测试只覆盖了"已暂停"的早退分支，没走到 _continuation）。
"""
import json
import threading
import urllib.request

import pytest

from proto.fleet.manager import AgentManager
from proto.web.server import ProtoWebServer
from proto.work.journal import GoalJournal
from proto.runtime.agent import ProtoAgent
from tests.test_web import call


class TestTemplateEcho:
    def test_prompt_template_echo_dropped(self, tmp_path):
        j = GoalJournal(tmp_path / "J.md")

        def fake(prompt, material):
            return ("发现：（本轮摸清的关键事实）\n"
                    "产出：（写出/修改的文件，没有则写 无）\n"
                    "下一步：（下一轮第 1 步的具体动作）")
        j.finish("failed", [], [], summarize=fake)
        head = j.resume_head()
        assert "本轮摸清的关键事实" not in head
        assert "写出/修改的文件" not in head
        assert "的具体动作）" not in head
        # 回显全被丢弃后必须有机械摘要兜底，不允许空发现
        assert "发现：" in head and head.split("发现：")[1].split("\n")[0].strip() not in ("", "无")

    def test_reworded_echo_variant_dropped(self, tmp_path):
        j = GoalJournal(tmp_path / "J.md")

        def fake(prompt, material):
            return "发现：（本轮摸清的关键事实：见上文）\n下一步：读 DESIGN.md 后补 Views"
        j.finish("max_steps", [], [], summarize=fake)
        head = j.resume_head()
        assert "关键事实：见上文" not in head
        assert "读 DESIGN.md" in head

    def test_legit_parenthesized_finding_kept(self, tmp_path):
        j = GoalJournal(tmp_path / "J.md")

        def fake(prompt, material):
            return "发现：（SwiftData 容器已配置 App Group）\n下一步：跑 swiftc -typecheck"
        j.finish("max_steps", [], [], summarize=fake)
        head = j.resume_head()
        assert "SwiftData 容器已配置 App Group" in head


class TestModelBlockCollapse:
    def test_consecutive_blocks_collapse(self, tmp_path):
        j = GoalJournal(tmp_path / "J.md")
        j.note_model_block([])
        j.note_model_block([])
        j.note_model_block([])
        assert j.round_count() == 1  # 状态未变不刷屏

    def test_block_with_files_still_noted(self, tmp_path):
        j = GoalJournal(tmp_path / "J.md")
        trace = [{"tool": "write_file", "args": {"path": "projects/x/a.swift"},
                  "result": "written:projects/x/a.swift", "ok": True}]
        j.note_model_block([])          # 先一条纯受阻
        j.note_model_block(trace)      # 有落盘的受阻仍要记（半程进度）
        head = j.resume_head()
        assert j.round_count() == 2
        assert "projects/x/a.swift" in head

    def test_recovery_then_reblock_appends(self, tmp_path):
        j = GoalJournal(tmp_path / "J.md")
        j.note_model_block([])
        j.note_round("awaiting_retry", ["正常一轮"], ["p/a"], ["b"])
        j.note_model_block([])
        assert j.round_count() == 3  # 恢复后的再次受阻是状态变化，要记


class TestActivityDetail:
    def test_dict_payload_serialized_readable(self):
        act = AgentManager.activity_of(
            [{"event_type": "mute_off", "payload": {"who": "owner"}}])
        assert act["detail"] == "who=owner"

    def test_empty_dict_not_braces(self):
        act = AgentManager.activity_of([{"event_type": "mute_off", "payload": {}}])
        assert act["detail"] not in ("{}", "")

    def test_string_payload_unchanged(self):
        act = AgentManager.activity_of([{"event_type": "task", "payload": "写 App"}])
        assert act["detail"] == "写 App"


class TestLiveRouteQueuedContinuation:
    """生产形态：未暂停的 agent + 模型退避中的排队续跑 run。"""

    @pytest.fixture()
    def server(self, tmp_path):
        agent = ProtoAgent(data_dir=tmp_path / "m80", llm=None, goal_interval_s=0)
        srv = ProtoWebServer(agent, host="127.0.0.1", port=0, heartbeat_s=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        yield srv
        srv.shutdown()
        srv.server_close()

    def test_unpaused_queued_continuation_does_not_500(self, server):
        import time as _time
        agent = server.manager.get(server.manager.current_id())
        runs = server.manager.runs
        # 先暂停防止 claimer 抢跑，落好"排队续跑 + 未来重试时间"后再恢复，
        # 复刻生产形态：agent 未暂停、run 排队等模型。
        server.manager.pause(agent.fleet_id, True)
        try:
            run = runs.enqueue(agent.fleet_id, "tick", {})
            with runs.database.connect(write=True) as connection:
                connection.execute(
                    "UPDATE runs SET status='queued', reason='model_retry_wait', result=?, "
                    "decision=NULL, args='{}' WHERE id = ?",
                    (json.dumps({"resume_session": True, "status": "waiting_model",
                                 "model_reason": "model_rate_limited",
                                 "retry_at": _time.time() + 3600}), run["id"]))
        finally:
            server.manager.pause(agent.fleet_id, False)
        status, feed = call(server, "GET", "/api/activity/live", token=server.token)
        assert status == 200, feed
        assert feed["run"]["status"] == "queued"
        assert feed["blocked"] == "model_retry_wait"

    def test_continuation_tolerates_projection_row(self, server):
        """快照投影（无 result 列）直接进 blocked_reason 不再 KeyError。"""
        agent = server.manager.get(server.manager.current_id())
        runs = server.manager.runs
        row = {"status": "queued", "reason": "model_retry_wait", "agent_id": agent.fleet_id,
               "work_id": None, "kind": "tick", "decision": None, "args": "{}"}
        # 投影行判不出续跑形态时安全返回字符串（曾在此 KeyError → 500）
        assert isinstance(runs.blocked_reason(row), str)

    def test_limit_param_honored_and_bounded(self, server):
        agent = server.manager.get(server.manager.current_id())
        runs = server.manager.runs
        for i in range(3):
            runs.enqueue(agent.fleet_id, "task", {"text": f"t{i}"})
        status, data = call(server, "GET", "/api/runs?limit=2", token=server.token)
        assert status == 200 and len(data["runs"]) == 2
        assert call(server, "GET", "/api/runs?limit=0", token=server.token)[0] == 400
        assert call(server, "GET", "/api/runs?limit=999", token=server.token)[0] == 400
        assert call(server, "GET", "/api/runs?limit=abc", token=server.token)[0] == 400


class TestDiagnosticLoopGuards:
    """生产实证（2026-11-23 run 250）：跑校验→读文件交替 24 步零写入。
    旧探索配额只数只读工具，run_command 把计数清零，交替循环永不触发。"""

    @pytest.fixture()
    def agent(self, tmp_path):
        from proto.tools.webtools import WebToolkit
        from proto.runtime.agent import ProtoAgent
        dead = WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))
        return ProtoAgent(data_dir=tmp_path, llm=None, goal_interval_s=10**9,
                          webtools=dead)

    @staticmethod
    def _failing_check():
        return {"tool": "run_command",
                "args": {"argv": ["python3", "-c",
                                  "import sys; print('FAIL: 界面清单屏数不足'); sys.exit(1)"]},
                "category": "workspace_write"}

    def test_alternating_diagnostic_loop_forced_to_write(self, agent, tmp_path):
        from proto.exec.agentic import AgenticLoop
        (tmp_path / "README.md").write_text("内容", encoding="utf-8")

        class LoopLLM:
            def __init__(self):
                self.prompts = []

            def complete(self, system, user):
                self.prompts.append((system, user))
                seq = agent.llm_seq.pop(0)
                return seq if isinstance(seq, str) else json.dumps(seq)

        check = self._failing_check()
        read = {"tool": "read_file", "args": {"path": "README.md"}, "category": "read_draft"}
        agent.llm_seq = [
            "干",
            check, read, dict(check), dict(read), dict(check), dict(read), dict(check),
            {"done": True, "summary": "被迫收工"},
            "发现：x\n产出：无\n下一步：改文件",
        ]
        llm = LoopLLM()
        agent.llm = llm
        result = AgenticLoop(agent, max_steps=10).run("实现某功能")
        obs = "\n".join(result["observations"])
        assert "连续 4 步没有落盘" in obs, obs[-800:]
        assert "3 次以相同方式失败" in obs, obs[-800:]

    def test_write_step_resets_counter_under_new_semantics(self, agent, tmp_path):
        from proto.exec.agentic import AgenticLoop
        (tmp_path / "README.md").write_text("内容", encoding="utf-8")

        class LoopLLM:
            def __init__(self):
                self.prompts = []

            def complete(self, system, user):
                self.prompts.append((system, user))
                seq = agent.llm_seq.pop(0)
                return seq if isinstance(seq, str) else json.dumps(seq)

        read = {"tool": "read_file", "args": {"path": "README.md"}, "category": "read_draft"}
        write = {"tool": "write_file", "args": {"path": "a.py", "content": "x=1"},
                 "category": "workspace_write"}
        agent.llm_seq = [
            "干", read, read, read, write, read, read, read,
            {"done": True, "summary": "ok"},
            "发现：ok\n产出：a.py\n下一步：done",
        ]
        llm = LoopLLM()
        agent.llm = llm
        result = AgenticLoop(agent, max_steps=10).run("t")
        # 3+1(write)+3：从未连续 4 步无落盘，不应出现强制提示
        assert not any("连续 4 步没有落盘" in o for o in result["observations"])
