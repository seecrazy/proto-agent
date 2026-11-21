import json
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from proto.exec.agentic import AgenticLoop, PLAN_PROMPT
from proto.exec.context import ContextError, create_context, render_context
from proto.exec.contracts import ParameterError, builtin_parameters, object_schema
from proto.exec.router import Task, ToolRegistry
from proto.exec.webtools import WebToolkit
from proto.kernel.kernel import ActionCategory
from proto.runtime.agent import ProtoAgent


class ScriptModel:
    def __init__(self, decisions=()):
        self.decisions = list(decisions)
        self.calls = []
        self.pass_verification = True

    def complete(self, system, user):
        self.calls.append((system, user))
        if "任务执行循环" in system:
            return json.dumps(self.decisions.pop(0), ensure_ascii=False)
        if "独立验收" in system:
            return json.dumps({"verdict": "pass" if self.pass_verification else "fail",
                               "missing": [] if self.pass_verification else ["缺少真实文件"]})
        if "最小可检验定义" in system:
            return json.dumps({"acceptance": "存在本机报告"})
        return "下一步写报告"


def make_agent(root, model=None):
    agent = ProtoAgent(data_dir=root, llm=model or ScriptModel(),
                       webtools=WebToolkit(fetch=lambda *args: (_ for _ in ()).throw(OSError("offline"))),
                       goal_interval_s=10**9)
    agent.llm_fast = None
    return agent


@pytest.fixture()
def agent(tmp_path):
    return make_agent(tmp_path)


def decision(tool, **arguments):
    return {"tool": tool, "args": arguments}


def register_external(agent, calls):
    def external(value: str):
        calls.append(value)
        return f"effect:{value}"
    agent.tools.register("external", external, category=ActionCategory.VISIBLE_WRITE,
                         owner_only=True,
                         parameters=object_schema({"value": {"type": "string"}}, ["value"]))


def pause(agent, calls, max_steps=6, goal_id="", pool="owner_tasks"):
    register_external(agent, calls)
    agent.llm.decisions = [decision("write_file", path="draft.md", content="evidence-before-pause"),
                           decision("external", value="approved-once")]
    result = AgenticLoop(agent, max_steps=max_steps, pool=pool).run(
        "完成唯一原任务", acceptance="固定验收：报告可读", goal_id=goal_id)
    assert result["status"] == "paused"
    assert calls == []
    return result


def test_actor_and_planner_share_constraints_not_chat_style(agent):
    agent.persona = "精密测量专家"
    agent.state.owner_priority = "必须交付测量证据"
    rule_id = agent.ledger.add("所有结论注明误差")
    for _ in range(5):
        agent.ledger.record_application(rule_id, "测量", True)
    agent.ledger.check_internalize(rule_id)
    agent.llm_fast = agent.llm
    agent.llm.decisions = [decision("clock"), {"done": True, "summary": "完成"}]
    result = AgenticLoop(agent).run("校对测量", acceptance="记录误差上界")
    assert result["status"] == "verified"
    relevant = [(system, user) for system, user in agent.llm.calls
                if system == PLAN_PROMPT or "任务执行循环" in system]
    assert len(relevant) == 3
    for system, user in relevant:
        assert all(value in user for value in ("精密测量专家", "所有结论注明误差",
                                               "必须交付测量证据", "记录误差上界", "owner_tasks"))
        assert "用简短自然的中文回应主人" not in system


def test_project_instructions_load_root_to_project_only(agent):
    root = agent.code_workspace.root
    (root / "projects/demo").mkdir(parents=True)
    (root / "projects/other").mkdir()
    (root / "AGENTS.md").write_text("root-rule", encoding="utf-8")
    (root / "projects/AGENTS.md").write_text("projects-rule", encoding="utf-8")
    (root / "projects/demo/AGENTS.md").write_text("demo-rule", encoding="utf-8")
    (root / "projects/other/AGENTS.md").write_text("unrelated-rule", encoding="utf-8")
    (agent.data_dir / "AGENTS.md").write_text("host-parent-secret", encoding="utf-8")
    context = create_context(agent, "任务", "验收", "owner_tasks", project_dir="projects/demo")
    rendered = render_context(context)
    assert rendered.index("root-rule") < rendered.index("projects-rule") < rendered.index("demo-rule")
    assert "unrelated-rule" not in rendered
    assert "host-parent-secret" not in rendered


@pytest.mark.parametrize("content", [b"x" * 16385, b"\xff\xfe"])
def test_invalid_project_instructions_fail_closed(agent, content):
    (agent.code_workspace.root / "AGENTS.md").write_bytes(content)
    result = AgenticLoop(agent).run("不得跳过规则")
    assert result["status"] == "failed"
    assert "context_error" in result["reason"]
    assert not agent.llm.calls


def test_instruction_symlink_is_not_followed(agent, tmp_path):
    secret = tmp_path / "host-instructions"
    secret.write_text("outside", encoding="utf-8")
    (agent.code_workspace.root / "AGENTS.md").symlink_to(secret)
    with pytest.raises(ContextError, match="symlink"):
        create_context(agent, "任务", "验收", "owner_tasks")


def test_nested_rules_are_seen_before_write_and_do_not_bleed_to_sibling(agent):
    root = agent.code_workspace.root
    (root / "first").mkdir()
    (root / "second").mkdir()
    (root / "first/AGENTS.md").write_text("ONLY-FIRST-RULE", encoding="utf-8")
    (root / "second/AGENTS.md").write_text("ONLY-SECOND-RULE", encoding="utf-8")
    first = decision("write_file", path="first/result.md", content="one")
    second = decision("write_file", path="second/result.md", content="two")
    agent.llm.decisions = [first, first, second, second, {"done": True}]
    result = AgenticLoop(agent, max_steps=6).run("分别写两份产物")
    users = [user for system, user in agent.llm.calls if "任务执行循环" in system]
    assert "ONLY-FIRST-RULE" not in users[0]
    assert "ONLY-FIRST-RULE" in users[1]
    assert "ONLY-SECOND-RULE" in users[3] and "ONLY-FIRST-RULE" not in users[3]
    assert [row["tool"] for row in result["trace"]] == ["write_file", "write_file"]
    assert len([event for event in agent.sessions.lookup(result["session_id"])["events"]
                if event["kind"] == "tool_result"]) == 2


def test_builtin_catalog_is_the_validation_source_and_is_defensive(agent):
    definitions = json.loads(AgenticLoop(agent)._tool_catalog())
    assert {row["name"] for row in definitions} == agent.tools.names()
    for row in definitions:
        assert row["parameters"] == builtin_parameters(row["name"])
    descriptor = agent.tools.definition("read_file")
    descriptor["parameters"]["required"].clear()
    with pytest.raises(ParameterError, match="path"):
        agent.tools.validate("read_file", {})


@pytest.mark.parametrize("tool,arguments", [
    ("write_file", {"path": "bad.md"}),
    ("write_file", {"path": "bad.md", "content": 4}),
    ("clock", {"unknown": 1}),
    ("read_file", {"path": "x", "offset": True}),
    ("read_file", {"path": "x", "limit": 501}),
    ("read_files", {"paths": ["x"] * 6}),
    ("read_files", {"paths": [False]}),
    ("edit_file", {"path": "x", "old": "a", "new": "b", "replace_all": "false"}),
    ("run_python", {"code": "pass", "timeout_s": float("nan")}),
    ("run_python", {"code": "pass", "timeout_s": -1}),
    ("run_command", {"argv": "python3 -V"}),
    ("run_command", {"argv": [True]}),
    ("run_command", {"argv": ["python3"] * 33}),
    ("blackboard_post", {"text": "test", "kind": "publish"}),
])
def test_contract_errors_precede_approval_and_side_effects(agent, tool, arguments):
    result = agent.router.run(Task("invalid", "不能先写草稿", [{
        "tool": tool, "args": arguments, "category": "visible_write"}]))
    assert result.status == "completed"
    assert "参数错误" in str(result.step_results)
    assert agent.queue.pending() == []
    assert agent.workspace.list_scratch() == []
    assert not (agent.code_workspace.root / "bad.md").exists()


def test_bad_arguments_can_be_corrected_in_same_loop(agent):
    agent.llm.decisions = [decision("write_file", path="ok.md", content=False),
                           decision("write_file", path="ok.md", content="fixed"),
                           {"done": True}]
    result = AgenticLoop(agent).run("纠正参数")
    assert result["status"] == "completed"
    assert len(result["trace"]) == 1
    assert any("参数错误" in item for item in result["observations"])
    assert (agent.code_workspace.root / "ok.md").read_text() == "fixed"


def test_custom_signature_kwargs_aliases_and_unsupported_schema(agent):
    registry = ToolRegistry()
    registry.register("custom", lambda text, enabled=False: text)
    assert registry.definition("custom")["parameters"]["required"] == ["text"]
    with pytest.raises(ParameterError):
        registry.validate("custom", {"text": "x", "enabled": "false"})
    registry.register("dynamic", lambda **payload: payload)
    assert registry.validate("dynamic", {"arbitrary": 42}) == {"arbitrary": 42}
    with pytest.raises(ValueError, match="unsupported"):
        registry.register("unsupported", lambda: None, parameters={"type": "object", "$ref": "any"})
    assert agent.tools.validate("write_file", {"filename": "legacy.md", "content": "ok"}) == {
        "path": "legacy.md", "content": "ok"}


def test_restart_resumes_original_loop_context_and_budget(agent, tmp_path):
    calls = []
    node = agent.goal_tree.add("原自主目标", "task", source="self", acceptance="固定验收：报告可读")
    paused = pause(agent, calls, goal_id=node.id, pool="self_initiated")
    saved = agent.sessions.lookup(paused["session_id"])
    assert saved["next_step"] == 2 and len(saved["trace"]) == 1
    replacement = make_agent(tmp_path, ScriptModel([
        decision("write_file", path="after.md", content="continued"), {"done": True, "summary": "原任务完成"}]))
    register_external(replacement, calls)
    before = replacement.kernel.pools.remaining("owner_tasks", "tokens")
    result = replacement.resume_task(paused["task_id"], True)
    assert result["status"] == "verified"
    assert result["session_id"] == paused["session_id"]
    assert len(result["trace"]) == 3
    assert calls == ["approved-once"]
    assert replacement.goal_tree.get(node.id).status == "done"
    assert replacement.kernel.pools.remaining("owner_tasks", "tokens") == before
    user = next(user for system, user in replacement.llm.calls if "任务执行循环" in system)
    assert all(value in user for value in ("完成唯一原任务", "固定验收：报告可读", "draft.md", "approved-once"))
    assert not any(system == PLAN_PROMPT for system, user in replacement.llm.calls)
    assert replacement.resume_task(paused["task_id"], True)["status"] == "failed"
    assert calls == ["approved-once"]


def test_resume_keeps_original_step_limit(agent):
    calls = []
    paused = pause(agent, calls, max_steps=2)
    agent.llm.decisions = [{"done": True}]
    result = agent.resume_task(paused["task_id"], True)
    assert result["status"] == "max_steps"
    assert calls == ["approved-once"]
    assert agent.llm.decisions == [{"done": True}]


def test_resume_can_pause_again_without_stale_approval_authority(agent):
    calls = []
    paused = pause(agent, calls)
    agent.llm.decisions = [decision("external", value="second"), {"done": True}]
    again = agent.resume_task(paused["task_id"], True)
    assert again["status"] == "paused"
    assert agent.resume_task(paused["task_id"], True)["reason"] == "stale_approval_task"
    assert calls == ["approved-once"]
    assert agent.resume_task(again["task_id"], True)["status"] == "verified"
    assert calls == ["approved-once", "second"]


def test_denial_does_not_resume_or_reward(agent):
    calls = []
    paused = pause(agent, calls)
    before = dict(agent.state.emotion)
    agent.llm.calls.clear()
    assert agent.resume_task(paused["task_id"], False)["status"] == "denied"
    assert agent.resume_task(paused["task_id"], True)["reason"] == "not_paused"
    assert not calls and not agent.llm.calls
    assert agent.state.emotion == before


def test_workspace_switch_blocks_before_approval(agent, tmp_path):
    calls = []
    paused = pause(agent, calls)
    agent.set_projects_root(str(tmp_path / "another-root"))
    result = agent.resume_task(paused["task_id"], True)
    assert result["reason"] == "workspace_changed"
    assert not calls and agent.queue.pending()


def test_dropped_goal_cannot_resume_pending_effect(agent):
    calls = []
    node = agent.goal_tree.add("可丢弃", "task", source="self")
    paused = pause(agent, calls, goal_id=node.id, pool="self_initiated")
    agent.goal_tree.drop(node.id)
    assert agent.resume_task(paused["task_id"], True)["status"] == "cancelled"
    assert not calls and agent.goal_tree.get(node.id).status == "dropped"


@pytest.mark.parametrize("field,value", [("schema_version", 99), ("variant_id", "other@1")])
def test_session_version_mismatch_is_fail_closed(agent, field, value):
    calls = []
    paused = pause(agent, calls)
    session = agent.sessions.lookup(paused["session_id"])
    session[field] = value
    agent.sessions.save(session)
    assert agent.resume_task(paused["task_id"], True)["reason"] == "session_version_mismatch"
    assert not calls and agent.queue.pending()


def test_policy_change_requires_reconciliation_not_old_approval(agent):
    calls = []
    paused = pause(agent, calls)
    agent.tools.register("external", lambda value: calls.append(value),
                         category=ActionCategory.IRREVERSIBLE, owner_only=True)
    result = agent.resume_task(paused["task_id"], True)
    assert result["status"] == "needs_reconciliation"
    assert result["reason"] == "tool_policy_changed"
    assert not calls and agent.queue.pending()


@pytest.mark.parametrize("approved,status", [(True, "verified"), (False, "denied")])
def test_completed_router_checkpoint_is_acknowledged_not_replayed(agent, monkeypatch, approved, status):
    calls = []
    register_external(agent, calls)
    agent.llm.decisions = [decision("external", value="once")]
    original = agent.router.run

    def interrupted(task):
        result = original(task)
        return agent.router.resume(task, True)

    monkeypatch.setattr(agent.router, "run", interrupted)
    original_observe = AgenticLoop._observe

    def crash_before_ack(loop, result):
        raise RuntimeError("crash after durable tool result")

    monkeypatch.setattr(AgenticLoop, "_observe", crash_before_ack)
    with pytest.raises(RuntimeError, match="crash"):
        AgenticLoop(agent).run("崩溃恢复", acceptance="结果可核对")
    session = agent.spine.store.find("execution_sessions", lambda row: True)[0]
    monkeypatch.setattr(AgenticLoop, "_observe", original_observe)
    agent.llm.decisions = [{"done": True}]
    if not approved:
        agent.llm = None
    assert agent.resume_task(session["id"], approved)["status"] == status
    assert calls == ["once"]


def test_unknown_in_flight_effect_is_not_retried(agent, monkeypatch):
    calls = []
    register_external(agent, calls)
    agent.llm.decisions = [decision("external", value="unknown")]

    def interrupted(task):
        calls.append("unknown")
        raise RuntimeError("lost result")

    monkeypatch.setattr(agent.router, "run", interrupted)
    with pytest.raises(RuntimeError):
        AgenticLoop(agent).run("未知副作用")
    session = agent.spine.store.find("execution_sessions", lambda row: True)[0]
    assert agent.resume_task(session["id"], True)["status"] == "needs_reconciliation"
    assert calls == ["unknown"]
    assert agent._current_op is None


def test_concurrent_resume_executes_pending_tool_once(agent):
    calls = []
    paused = pause(agent, calls)
    agent.llm.decisions = [{"done": True}]
    results = []
    workers = [threading.Thread(target=lambda: results.append(agent.resume_task(paused["task_id"], True)))
               for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)
    assert len(results) == 2
    assert sorted(result["status"] for result in results) == ["failed", "verified"]
    assert calls == ["approved-once"]


def test_restored_context_keeps_identity_acceptance_and_instruction_snapshot(agent):
    calls = []
    agent.persona = "ORIGINAL-PERSONA"
    source = agent.code_workspace.root / "AGENTS.md"
    source.write_text("ORIGINAL-RULE", encoding="utf-8")
    paused = pause(agent, calls)
    source.write_text("CHANGED-RULE", encoding="utf-8")
    restored = make_agent(agent.data_dir, ScriptModel([{"done": True}]))
    restored.persona = "CHANGED-PERSONA"
    register_external(restored, calls)
    assert restored.resume_task(paused["task_id"], True)["status"] == "verified"
    user = next(user for system, user in restored.llm.calls if "任务执行循环" in system)
    assert all(value in user for value in ("ORIGINAL-PERSONA", "ORIGINAL-RULE", "固定验收：报告可读"))
    assert "CHANGED-PERSONA" not in user and "CHANGED-RULE" not in user


def test_instruction_total_limit_fails_before_model(agent):
    folder = agent.code_workspace.root
    for name in ("", "a", "b", "c", "d"):
        folder = folder / name
        folder.mkdir(exist_ok=True)
        (folder / "AGENTS.md").write_text("x" * 16384, encoding="utf-8")
    result = AgenticLoop(agent).run("不截断约束", project_dir="a/b/c/d")
    assert result["status"] == "failed" and "instruction_too_large" in result["reason"]
    assert not agent.llm.calls


@pytest.mark.parametrize("project_dir", ["../escape", "/tmp"])
def test_project_instruction_scope_cannot_escape_workspace(agent, project_dir):
    result = AgenticLoop(agent).run("禁止越界读取指令", project_dir=project_dir)
    assert result["status"] == "failed" and "outside_workspace" in result["reason"]
    assert not agent.llm.calls


@pytest.mark.parametrize("field,value", [
    ("next_step", True), ("next_step", 100), ("max_steps", -1),
    ("status", []), ("prompt_blocks", ["incomplete"]),
    ("context.pool", "global"), ("pending_call.args", "not-an-object"),
])
def test_invalid_session_is_rejected_before_approval(agent, field, value):
    calls = []
    paused = pause(agent, calls)
    session = agent.sessions.lookup(paused["session_id"])
    if "." in field:
        parent, name = field.split(".")
        session[parent][name] = value
    else:
        session[field] = value
    agent.sessions.save(session)
    assert agent.resume_task(paused["task_id"], True)["reason"] == "invalid_execution_session"
    assert not calls and agent.queue.pending()


@pytest.mark.parametrize("field,value", [("schema_version", 99), ("schema_version", True),
                                        ("variant_id", "other@1")])
def test_checkpoint_version_is_checked_before_approval(agent, field, value):
    calls = []
    paused = pause(agent, calls)
    checkpoint = agent.checkpoints.resume(paused["task_id"])
    checkpoint[field] = value
    agent.spine.store.put("checkpoints", paused["task_id"], checkpoint)
    assert agent.resume_task(paused["task_id"], True)["reason"] == "checkpoint_version_mismatch"
    assert not calls and agent.queue.pending()


def test_stricter_parameters_prevent_old_approval_but_allow_denial(agent):
    calls = []
    paused = pause(agent, calls)
    agent.tools.register("external", lambda value: calls.append(value),
                         category=ActionCategory.VISIBLE_WRITE, owner_only=True,
                         parameters=object_schema({"value": {"type": "integer"}}, ["value"]))
    result = agent.resume_task(paused["task_id"], True)
    assert result["status"] == "needs_reconciliation" and "parameter_contract" in result["reason"]
    assert not calls and agent.queue.pending()
    assert agent.resume_task(paused["task_id"], False)["status"] == "denied"
    assert not calls and not agent.queue.pending()


def test_denial_works_offline_after_workspace_and_policy_change(agent, tmp_path):
    calls = []
    paused = pause(agent, calls)
    agent.llm = None
    agent.code_workspace.root = tmp_path / "other-workspace"
    agent.tools.register("external", lambda value: calls.append(value),
                         category=ActionCategory.IRREVERSIBLE, owner_only=True)
    assert agent.resume_task(paused["task_id"], False)["status"] == "denied"
    assert not calls and not agent.queue.pending()


def test_journal_scope_is_validated_before_approval(agent):
    calls = []
    paused = pause(agent, calls)
    session = agent.sessions.lookup(paused["session_id"])
    session["journal_path"] = str(agent.data_dir.parent / "outside-journal.md")
    agent.sessions.save(session)
    result = agent.resume_task(paused["task_id"], True)
    assert result["reason"] == "journal_path_outside_data"
    assert not calls and agent.queue.pending()


def test_checkpoint_must_match_original_pending_call(agent):
    calls = []
    paused = pause(agent, calls)
    checkpoint = agent.checkpoints.resume(paused["task_id"])
    checkpoint["state"]["pending_step"]["args"] = {"value": "different"}
    agent.spine.store.put("checkpoints", paused["task_id"], checkpoint)
    assert agent.resume_task(paused["task_id"], True)["reason"] == "checkpoint_call_mismatch"
    assert not calls and agent.queue.pending()


def test_pending_goal_is_not_restarted_by_another_work_request(agent):
    calls = []
    node = agent.goal_tree.add("同一目标", "task", source="self")
    paused = pause(agent, calls, goal_id=node.id, pool="self_initiated")
    before = len(agent.llm.calls)
    result = agent.work_on_goal(node)
    assert result["status"] == "paused" and result["session_id"] == paused["session_id"]
    assert len(agent.llm.calls) == before and not calls
    assert len(agent.spine.store.find("execution_sessions", lambda row: True)) == 1


def test_failed_verification_after_resume_does_not_complete_goal_or_reward(agent, monkeypatch):
    calls, events = [], []
    node = agent.goal_tree.add("保持未完成", "task", source="self")
    paused = pause(agent, calls, goal_id=node.id, pool="self_initiated")
    monkeypatch.setattr(agent.pad, "apply_event", events.append)
    agent.llm.decisions = [{"done": True}]
    agent.llm.pass_verification = False
    result = agent.resume_task(paused["task_id"], True)
    assert result["status"] == "awaiting_retry" and result["verified"] is False
    assert agent.goal_tree.get(node.id).status == "active"
    assert "task_success" not in events


def test_tool_result_is_durable_before_prompt_clipping(agent):
    calls = []
    register_external(agent, calls)
    agent.tools.register("large_read", lambda: "FULL-RESULT" * 1200)
    agent.llm.decisions = [decision("large_read"), decision("external", value="pause")]
    paused = AgenticLoop(agent).run("保存完整工具事件")
    session = agent.sessions.lookup(paused["session_id"])
    result = next(event for event in session["events"] if event["kind"] == "tool_result")
    assert len(result["results"][0]) == 13200
    assert len(session["trace"][0]["result"]) < 1000


def test_repeated_call_guard_survives_multiple_approvals(agent):
    calls = []
    paused = pause(agent, calls)
    agent.llm.decisions = [decision("external", value="approved-once")]
    second = agent.resume_task(paused["task_id"], True)
    assert second["status"] == "paused"
    agent.llm.decisions = [decision("external", value="approved-once"), {"done": True}]
    final = agent.resume_task(second["task_id"], True)
    assert final["status"] == "verified"
    assert calls == ["approved-once", "approved-once"]
    assert any("重复调用" in observation for observation in final["observations"])


@pytest.mark.parametrize("change", ["workspace", "goal"])
def test_context_change_during_model_call_stops_before_effect(agent, change):
    node = agent.goal_tree.add("模型等待期间可停止", "task", source="self")
    agent.llm.decisions = [decision("write_file", path="no-effect.md", content="unsafe")]
    original = agent.llm.complete
    original_root = agent.code_workspace.root

    def changed(system, user):
        raw = original(system, user)
        if "任务执行循环" in system:
            if change == "goal":
                agent.goal_tree.drop(node.id)
            else:
                agent.code_workspace.root = agent.data_dir / "another-root"
        return raw

    agent.llm.complete = changed
    result = AgenticLoop(agent, pool="self_initiated").run("不得执行过期决策", goal_id=node.id)
    assert result["status"] == ("cancelled" if change == "goal" else "needs_reconciliation")
    assert not (original_root / "no-effect.md").exists()
    assert not (agent.code_workspace.root / "no-effect.md").exists()
    assert agent._current_op is None


@pytest.mark.parametrize("source", ["owner", "self"])
def test_verification_retry_returns_its_new_approval_session(agent, source):
    calls = []
    register_external(agent, calls)
    agent.llm_fast = agent.llm
    agent.llm.pass_verification = False
    agent.llm.decisions = [{"done": True}, decision("external", value="retry-approval")]
    if source == "owner":
        result = agent.run_task("验收重试可能暂停")
    else:
        node = agent.goal_tree.add("验收重试可能暂停", "task", source="self")
        result = agent.work_on_goal(node)
        assert agent.goal_tree.get(node.id).status == "active"
        assert result["status"] == "awaiting_retry"
        assert agent.llm.decisions == [decision("external", value="retry-approval")]
        assert not agent.queue.pending()
        result = agent.work_on_goal(node)
    assert result["status"] == "paused"
    assert bool(result.get("retried")) is (source == "owner")
    assert result["task_id"] == agent.queue.pending()[0].task_id
    assert agent.sessions.lookup(result["session_id"])["status"] == "awaiting_approval"
    assert not calls


@pytest.mark.parametrize("action,status", [("accept", "verified"), ("deny", "denied")])
def test_proposal_http_entry_resumes_original_session(agent, action, status):
    from proto.web.server import ProtoWebServer
    calls = []
    node = agent.goal_tree.add("控制台审批原任务", "task", source="self")
    paused = pause(agent, calls, goal_id=node.id, pool="self_initiated")
    server = ProtoWebServer(agent, host="127.0.0.1", port=0, heartbeat_s=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def request(path, method="GET"):
        payload = b"{}" if method == "POST" else None
        req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", data=payload, method=method,
                                     headers={"X-Proto-Token": server.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.loads(response.read())

    try:
        proposal = request("/api/proposals")["proposals"][0]
        assert proposal["resumable"] and proposal["session_id"] == paused["session_id"]
        assert proposal["step"]["args"] == {"value": "approved-once"}
        agent.llm.decisions = [{"done": True}]
        result = request(f"/api/proposals/{paused['proposal_id']}/{action}", "POST")
        if action == "accept":
            assert result["status"] == "verified" and result["session_id"] == paused["session_id"]
            assert calls == ["approved-once"] and agent.goal_tree.get(node.id).status == "done"
        else:
            assert result["denied"] == paused["proposal_id"]
            assert not calls and agent.goal_tree.get(node.id).status == "active"
        assert agent.sessions.lookup(paused["session_id"])["status"] == status
        assert not any(path.startswith("proposals/") for path in agent.workspace.list_scratch())
    finally:
        server.shutdown()
        worker.join(timeout=5)


def test_task_ui_keeps_latest_approval_and_verified_result():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the isolated UI behavior check")
    html = (Path(__file__).resolve().parent.parent / "proto/web/index.html").read_text(encoding="utf-8")
    script = re.search(r"let pausedTaskId = null;(.*?)async function addRule", html, re.S).group(1)
    harness = '''
const elements = Object.fromEntries(["task-result", "task-resume", "task-plan-review"].map(name => [name, {style: {}, textContent: ""}]));
globalThis.document = {getElementById: name => elements[name]};
globalThis.refreshSide = () => {};
const paths = [];
const replies = [{status: "paused", task_id: "second"}, {status: "verified", verified: true, summary: "actual-result"}];
globalThis.api = async (method, path) => { paths.push(path); return replies.shift(); };
let pausedTaskId = null;
'''
    assertions = '''
(async () => {
  renderTaskResult({status: "paused", task_id: "first"});
  await resumeTask(true);
  if (pausedTaskId !== "second" || elements["task-resume"].style.display !== "flex") throw Error("lost second approval");
  await resumeTask(true);
  if (pausedTaskId !== null || elements["task-resume"].style.display !== "none") throw Error("terminal approval remains");
  if (!elements["task-plan-review"].hidden) throw Error("stale plan review remains");
  if (!elements["task-result"].textContent.includes("actual-result")) throw Error("verified result missing");
  console.log(JSON.stringify(paths));
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run([node, "-e", harness + script + assertions], text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["/api/task/first/resume", "/api/task/second/resume"]


@pytest.mark.parametrize("invalid", ["false", 1, 0, None, []])
def test_approval_requires_real_boolean_in_http_and_runtime(agent, invalid):
    from proto.web.server import ProtoWebServer
    calls = []
    paused = pause(agent, calls)
    before = len(agent.llm.calls)
    assert agent.resume_task(paused["task_id"], invalid)["reason"] == "invalid_approval_decision"
    assert AgenticLoop(agent).resume(paused["task_id"], invalid)["reason"] == "invalid_approval_decision"
    assert agent.router.resume(Task(paused["task_id"], "原任务", []), invalid).reason == "invalid_approval_decision"
    server = ProtoWebServer(agent, host="127.0.0.1", port=0, heartbeat_s=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/api/task/{paused['task_id']}/resume",
            data=json.dumps({"approved": invalid}).encode(), method="POST",
            headers={"X-Proto-Token": server.token, "Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        assert error.value.code == 400
        assert len(agent.llm.calls) == before and not calls and agent.queue.pending()
        assert agent.sessions.lookup(paused["session_id"])["status"] == "awaiting_approval"
    finally:
        server.shutdown()
        worker.join(timeout=5)
