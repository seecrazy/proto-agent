import json
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError

import pytest

from proto.llm.breaker import CircuitBreaker, model_failure
from proto.llm.client import LLMClient, LLMError
from proto.memory.extract import EXTRACT_PROMPT
from proto.runtime.agent import ProtoAgent
from proto.runtime.coordination import CoordinationDB
from proto.runtime.dialogue import Dialogue
from proto.web.server import ProtoWebServer
from proto.work.delivery import main_project
from proto.work.handoff import lineage_handoff
from proto.work.journal import GoalJournal
from tests.test_web import call


class Scenario(LLMClient):
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block_model = False
        self.failed = False
        self.decision = None
        self.foreground = []
        self.decisions = 0

    def complete(self, system, user):
        if "任务执行循环" in system:
            self.decisions += 1
            if self.failed:
                raise TimeoutError("private provider detail")
            if self.block_model:
                self.entered.set()
                assert self.release.wait(15)
            if self.decision:
                decision, self.decision = self.decision, None
                return json.dumps(decision)
            return '{"done":true,"summary":"工作已检查"}'
        if "最小可检验定义" in system:
            return '{"acceptance":"必须有约定的证据"}'
        if "独立验收" in system:
            return '{"verdict":"pass"}'
        if "[后台工作事实" in user:
            self.foreground.append(user)
            return "这是同一个 Agent 的完整回复。"
        return "[]"


@pytest.fixture
def live(tmp_path):
    model = Scenario()
    agent = ProtoAgent(tmp_path / "agent", llm=model, fast_llm=Scenario(), persona="iPhone 开发专家")
    server = ProtoWebServer(agent, port=0, heartbeat_s=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, agent, model
    model.release.set()
    server.shutdown()
    server.server_close()
    thread.join(2)


def test_real_reply_and_status_during_stalled_background_model(live):
    server, agent, model = live
    runs = server.manager.runs
    model.block_model = True
    run = runs.enqueue(agent.fleet_id, "task", {"text": "整理构建证据"})
    try:
        assert model.entered.wait(4)
        status, feed = call(server, "GET", "/api/activity/live", token=server.token)
        assert status == 200 and feed["run"]["id"] == run["id"]
        assert feed["current"]["phase"] == "model_wait" and feed["step"] == 1
        response, question = call(server, "POST", "/api/chat", {"text": "在干嘛？"}, token=server.token)
        assert response == 200 and question["read_only"] and "等模型" in question["reply"]
        other = server.manager.create("独立 Agent")
        status, unassociated = call(server, "GET", "/api/activity/live", token=server.token, agent_id=other["id"])
        assert status == 200 and unassociated["run"] is None and not unassociated["events"]
        _, unauthenticated = call(server, "GET", "/api/activity/live")
        assert unauthenticated == {"error": "unauthorized"}

        message = {"text": "换个话题。你觉得这个交互怎样？"}
        status, reply = call(server, "POST", "/api/chat", message, token=server.token, request_key="turn-1")
        assert status == 200 and reply["reply"] == "这是同一个 Agent 的完整回复。"
        assert "整理构建证据" in model.foreground[0] and "正在等模型返回" in model.foreground[0]
        assert call(server, "POST", "/api/chat", message, token=server.token, request_key="turn-1") == (200, reply)
        assert call(server, "POST", "/api/chat", {"text": "不同文本"}, token=server.token,
                    request_key="turn-1") == (409, {"error": "idempotency_conflict"})
        assert model.decisions == len(model.foreground) == 1
        assert agent._load_history()[-2:][0]["text"] == message["text"]
        assert runs.get(run["id"])["status"] == "running"
        with runs.database.connect() as connection:
            tickets = connection.execute("SELECT run_id, state FROM model_tickets").fetchall()
        assert any(row["run_id"] == run["id"] and row["state"] == "reserved" for row in tickets)
        assert any(row["run_id"] is None and row["state"] == "settled" for row in tickets)
    finally:
        model.release.set()
    assert runs.wait(run["id"], timeout=5)["status"] == "succeeded"


def test_stalled_tool_does_not_block_chat_or_cancel(live, monkeypatch):
    server, agent, model = live
    model.decision = {"tool": "run_command", "args": {"argv": ["swift", "test"]}}

    def wait_for_tool(*args):
        model.entered.set()
        assert model.release.wait(12)
        return "sensitive output never in the activity stream"

    monkeypatch.setattr(agent.code_workspace, "run_command", wait_for_tool)
    runs = server.manager.runs
    run = runs.enqueue(agent.fleet_id, "task", {"text": "执行本地 Swift 验证"})
    try:
        assert model.entered.wait(4)
        _, feed = call(server, "GET", "/api/activity/live", token=server.token)
        assert feed["current"]["phase"] == "tool_start" and feed["current"]["tool"] == "run_command"
        _, reply = call(server, "POST", "/api/chat", {"text": "聊聊界面"}, token=server.token)
        assert reply["reply"].startswith("这是同一个 Agent")
        _, cancelled = call(server, "POST", f"/api/runs/{run['id']}/cancel", {}, token=server.token)
        assert cancelled["cancel_requested"]
    finally:
        model.release.set()
    assert runs.wait(run["id"], timeout=5)["status"] == "cancelled"
    assert model.decisions == 1
    assert "sensitive output" not in json.dumps(runs.activity.snapshot(agent))


def test_model_outage_pauses_automatic_runs_then_resumes_real_work(live, monkeypatch):
    server, agent, model = live
    runs = server.manager.runs
    model.failed = True
    goal = agent.goal_tree.add("实现 Wengu SwiftUI 功能", "mid", acceptance="Product.swift 存在")
    monkeypatch.setattr(agent, "maybe_proactive", lambda: agent.work_on_goal(goal))
    monkeypatch.setattr(agent, "_curate_methods", lambda *args, **kwargs: "")
    for _index in range(3):
        run = runs.enqueue(agent.fleet_id, "tick")
        result = runs.wait(run["id"], timeout=4)
        assert result["status"] == "failed" and not result["result"]["proposal"]["trace"]
    assert model.decisions == 3 and goal.fail_streak == 0
    for _index in range(4):
        server.manager.enqueue_ticks()
    assert len(runs.list(agent.fleet_id)) == 3
    feed = runs.activity.snapshot(agent)
    assert feed["model"]["waiting"] and feed["model"]["reason"] == "model_timeout"
    assert "private provider detail" not in json.dumps(feed)
    journal = GoalJournal(agent.data_dir / "work" / goal.id / "JOURNAL.md")
    assert journal.latest_round()["status"] == "model_blocked"
    assert journal.consecutive_no_output() == 0

    _, breaker = agent._model_binding(agent.llm)
    breaker._opened_at -= 601
    model.failed = False
    path = main_project(agent, goal) + "/Product.swift"
    model.decision = {"tool": "write_file", "args": {"path": path, "content": "struct Wengu {}"}}
    server.manager.enqueue_ticks()
    resumed = runs.list(agent.fleet_id)[0]
    assert runs.wait(resumed["id"], timeout=5)["status"] == "succeeded"
    assert (agent.code_workspace.root / path).read_text() == "struct Wengu {}"
    recovery_events = [event for event in runs.activity.snapshot(agent)["events"] if event["run_id"] == resumed["id"]]
    assert [event["phase"] for event in recovery_events if event["tool"] == "write_file"] == [
        "tool_start", "tool_returned", "file_written"]
    assert any(event["path"] == path for event in recovery_events if event["phase"] == "file_written")
    assert not agent.model_status()["waiting"]
    status, prior = call(server, "POST", "/api/chat", {"text": "你刚才做了什么？"}, token=server.token)
    assert status == 200 and prior["read_only"] and path in prior["reply"]
    assert "最近后台轮次" in prior["reply"] and not model.foreground and not agent._load_history()


def test_reopened_dialogue_keeps_receipt_and_remembers_valid_facts(tmp_path):
    class FactModel(Scenario):
        def complete(self, system, user):
            if system == EXTRACT_PROMPT:
                return json.dumps([{"text": "格式错误", "importance": []}, {"text": "我正在开发 Wengu"}])
            return super().complete(system, user)

    model = Scenario()
    data = tmp_path / "agent"
    original = ProtoAgent(data, llm=model)
    dialogue = Dialogue(CoordinationDB(tmp_path))
    message, key = "我正在开发 Wengu", "durable-turn"
    reply = dialogue.reply(original, message, key, lambda: "没有后台任务")
    assert dialogue.pending_agents() == [original.fleet_id] and not len(original.memory)

    restored_model = Scenario()
    restored = ProtoAgent(data, llm=restored_model, fast_llm=FactModel())
    reopened = Dialogue(CoordinationDB(tmp_path))
    assert reopened.reply(restored, message, key, lambda: "不应再次取快照") == reply
    assert len(model.foreground) == 1 and not restored_model.foreground
    assert reopened.remember_one(restored) and not reopened.remember_one(restored)
    assert not reopened.pending_agents() and restored.memory.snapshot()["episodes"] == 1
    assert [row["text"] for row in restored.memory.snapshot()["facts"]] == [message]


def test_half_open_sends_one_probe_and_ignores_stale_success():
    breaker = CircuitBreaker(fail_threshold=1, cooldown_s=0.1)
    original = breaker.acquire()
    breaker.record_failure("model_timeout", permit=original)
    breaker._opened_at -= 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        permits = list(pool.map(lambda _: breaker.acquire(), range(32)))
    assert sum(permit is not None for permit in permits) == 1
    breaker.record_success(permit=original)
    assert breaker.snapshot()["state"] == "half_open"
    breaker.record_success(permit=next(permit for permit in permits if permit is not None))
    assert breaker.snapshot()["state"] == "closed"


def test_http_failure_is_classified_without_exposing_provider_payload():
    try:
        try:
            raise HTTPError("https://example.invalid/internal", 403, "private payload", {}, None)
        except HTTPError as error:
            raise LLMError("private payload") from error
    except LLMError as error:
        assert model_failure(error) == "model_forbidden"


def test_handoff_uses_existing_sibling_outputs_not_other_projects(tmp_path):
    agent = ProtoAgent(tmp_path / "agent")
    parent = agent.goal_tree.add("iPhone 应用", "long")
    design = agent.goal_tree.add("设计", "mid", parent_id=parent.id)
    app = agent.goal_tree.add("SwiftUI 实现", "mid", parent_id=parent.id)
    tests = agent.goal_tree.add("XCTest 验证", "mid", parent_id=parent.id)
    other = agent.goal_tree.add("另一个项目", "mid")
    design_dir, app_dir, tests_dir, other_dir = (
        "projects/old-design", "projects/old-app", "projects/old-tests", "projects/other")
    for goal, directory in zip((design, app, tests, other), (design_dir, app_dir, tests_dir, other_dir)):
        agent.spine.store.put("project_dirs", goal.id, {"dir": directory})
    agent.code_workspace.write_file(design_dir + "/SPEC.md", "真实设计")
    agent.code_workspace.write_file(app_dir + "/WenguApp.swift", "import SwiftUI")
    agent.code_workspace.write_file(other_dir + "/unrelated.swift", "另一个项目")
    (agent.code_workspace.root / app_dir / "alias.swift").symlink_to(
        agent.code_workspace.root / other_dir / "unrelated.swift")
    agent.goal_tree.complete(design.id)
    text = lineage_handoff(agent, tests, tests_dir)
    assert "SPEC.md" in text and "该阶段已验收" in text
    assert "WenguApp.swift" in text and "待验收" in text
    assert "unrelated.swift" not in text and "alias.swift" not in text
    assert "真实设计" not in text
    assert (agent.code_workspace.root / app_dir / "WenguApp.swift").is_file()
