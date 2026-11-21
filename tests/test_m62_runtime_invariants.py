import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from proto.exec.agentic import AgenticLoop
from proto.exec.approval import ActionProposal
from proto.exec.codetools import CodeTools
from proto.exec.router import Task
from proto.exec.webtools import WebToolkit
from proto.goals.system import GoalTree
from proto.kernel.budgets import BudgetPools, parse_amount
from proto.kernel.kernel import ActionCategory
from proto.quality.gate import generate_acceptance, verify
from proto.runtime.agent import ProtoAgent
from proto.storage.store import FileSystemStore


class AuditLLM:
    def __init__(self, decisions=()):
        self.decisions = list(decisions)
        self.calls = []
        self.forge_rows = []
        self.children = [{"text": "实现本地工具", "acceptance": "存在 main.py"}]

    def complete(self, system, user):
        self.calls.append((system, user))
        if "GoalForge" in system:
            return json.dumps(self.forge_rows, ensure_ascii=False)
        if "目标分解器" in system:
            return json.dumps(self.children, ensure_ascii=False)
        if "独立验收" in system:
            return json.dumps({"verdict": "pass"})
        if "最小可检验定义" in system:
            return json.dumps({"acceptance": "存在本地报告"})
        if "任务执行循环" in system:
            decision = self.decisions.pop(0)
            return decision if isinstance(decision, str) else json.dumps(decision)
        if "已有方法库" in system:
            return json.dumps({"owner_hinted": [], "invented": []})
        if "归因" in system:
            return "[]"
        return "执行计划"


@pytest.fixture()
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTO_EXEC_BACKEND", "none")
    monkeypatch.setenv("PROTO_BRIDGE_CLI", "none")
    return ProtoAgent(tmp_path / "agent", llm=AuditLLM(), goal_interval_s=10 ** 9,
                      webtools=WebToolkit(fetch=lambda url, accept: (200, "offline")))


def visible_task(agent, task_id="visible", steps=None):
    calls = []
    agent.tools.register("external", lambda value: calls.append(value),
                         category=ActionCategory.VISIBLE_WRITE)
    task = Task(task_id, "受审批动作", steps or [
        {"tool": "external", "args": {"value": "approved"}}])
    return task, calls


def test_denied_decision_cannot_execute_on_resume(agent):
    task, calls = visible_task(agent)
    paused = agent.router.run(task)
    agent.queue.deny(paused.proposal_id, "owner_denied")
    result = agent.router.resume(task, human_approved=True)
    assert result.status == "denied"
    assert calls == []
    assert "waiting_for" not in agent.checkpoints.resume(task.id)["state"]


def test_denied_checkpoint_cannot_be_reapproved(agent):
    task, calls = visible_task(agent)
    agent.router.run(task)
    assert agent.resume_task(task.id, False)["status"] == "denied"
    assert agent.resume_task(task.id, True)["status"] == "failed"
    assert calls == []


def test_failed_resume_is_not_replayed(agent):
    calls = []

    def external():
        calls.append("started")
        raise RuntimeError("side effect outcome unknown")

    agent.tools.register("external", external, category=ActionCategory.VISIBLE_WRITE)
    task = Task("uncertain", "动作", [{"tool": "external", "args": {}}])
    agent.router.run(task)
    agent.resume_task(task.id, True)
    agent.resume_task(task.id, True)
    assert calls == ["started"]


def test_concurrent_resume_executes_side_effect_once(agent):
    entered = threading.Event()
    release = threading.Event()
    duplicate = threading.Event()
    calls, results = [], []

    def external():
        calls.append("started")
        if len(calls) > 1:
            duplicate.set()
        entered.set()
        release.wait(timeout=5)
        return "done"

    agent.tools.register("external", external, category=ActionCategory.VISIBLE_WRITE)
    task = Task("concurrent", "并发恢复", [{"tool": "external", "args": {}}])
    agent.router.run(task)

    def resume():
        results.append(agent.router.resume(task, human_approved=True).status)

    first = threading.Thread(target=resume, daemon=True)
    second = threading.Thread(target=resume, daemon=True)
    first.start()
    try:
        assert entered.wait(timeout=2)
        second.start()
        duplicate.wait(timeout=0.2)
    finally:
        release.set()
        first.join(timeout=5)
        if second.ident is not None:
            second.join(timeout=5)
    assert calls == ["started"]
    assert sorted(results) == ["completed", "failed"]


def test_irreversible_file_write_requires_approval(agent):
    task = Task("irreversible", "写文件", [{
        "tool": "write_file", "args": {"path": "report.md", "content": "safe"},
        "category": "irreversible"}])
    result = agent.router.run(task)
    assert result.status == "paused"
    assert agent.queue.pending()[0].category is ActionCategory.IRREVERSIBLE
    assert agent.workspace.list_visible() == []


def test_approved_file_uses_original_content_not_another_draft(agent):
    def task(task_id, content):
        return Task(task_id, "写报告", [{"tool": "write_file", "args": {
            "path": "report.md", "content": content}, "category": "visible_write"}])

    first = task("first", "first approved content")
    second = task("second", "unapproved second content")
    agent.router.run(first)
    agent.router.run(second)
    assert agent.resume_task(first.id, True)["status"] == "completed"
    visible = agent.data_dir / "ws" / "visible" / "report.md"
    assert visible.read_text() == "first approved content"


def test_resumed_tool_uses_same_parameter_contract_and_audit(agent):
    task, calls = visible_task(agent, steps=[{
        "tool": "external", "args": {"value": "approved", "unexpected": "ignored"}}])
    invalid = agent.router.run(task)
    assert "未知参数" in str(invalid.step_results)
    assert calls == []
    assert not agent.queue.pending()
    task.id = "corrected-parameters"
    task.steps[0]["args"] = {"value": "approved"}
    assert agent.router.run(task).status == "paused"
    result = agent.resume_task(task.id, True)
    assert result["status"] == "completed"
    assert calls == ["approved"]
    assert agent.spine.find(lambda row: row["event_type"] == "tool_exec")


def test_keyword_variadic_tool_keeps_arguments(agent):
    agent.tools.register("dynamic", lambda **payload: payload["answer"])
    result = agent.router.run(Task("kwargs", "参数", [{
        "tool": "dynamic", "args": {"answer": 42}}]))
    assert result.step_results[-1] == 42


def test_invalid_file_path_is_a_recoverable_observation(agent):
    result = agent.router.run(Task("badpath", "路径", [
        {"tool": "write_file", "args": {"path": "../escape", "content": "no"}},
        {"tool": "clock", "args": {}}]))
    assert result.status == "completed"
    assert "ValueError" in str(result.step_results[0])
    assert len(result.step_results) == 2


@pytest.mark.parametrize("decision", [
    "not JSON", "[]", '{"done": "false"}',
    '{"tool": "clock", "args": []}', '{"done": true, "tool": "clock"}',
])
def test_invalid_decision_is_not_completion(agent, decision):
    agent.llm.decisions = [decision]
    result = AgenticLoop(agent, max_steps=1).run("遵守结构化协议")
    assert result["status"] == "max_steps"
    assert result["observations"]
    assert agent._current_op is None


def test_loop_recovers_after_invalid_arguments(agent):
    agent.llm.decisions = [
        {"tool": "clock", "args": ["wrong"]}, {"done": True, "summary": "ok"}]
    result = AgenticLoop(agent).run("恢复")
    assert result["status"] == "completed"
    assert result["observations"]


def test_activity_marker_cleared_after_unexpected_error(agent, monkeypatch):
    agent.llm.decisions = [{"tool": "clock", "args": {}}]

    def fail(task):
        raise RuntimeError("unexpected dispatcher failure")

    monkeypatch.setattr(agent.router, "run", fail)
    with pytest.raises(RuntimeError):
        AgenticLoop(agent).run("清理活动标记")
    assert agent._current_op is None


def test_self_loop_does_not_consume_owner_pool(agent):
    before = agent.kernel.pools.remaining("owner_tasks", "tokens")
    before_calls = agent.kernel.pools.remaining("global", "calls")
    agent.llm.decisions = [
        {"tool": "clock", "args": {}}, {"done": True, "summary": "ok"}]
    result = AgenticLoop(agent, pool="self_initiated").run("自主验证", acceptance="可验证")
    assert result["status"] == "verified"
    assert agent.kernel.pools.remaining("owner_tasks", "tokens") == before
    assert before_calls - agent.kernel.pools.remaining("global", "calls") == 4


def test_acceptance_and_verification_calls_are_budgeted(agent):
    before_calls = agent.kernel.pools.remaining("global", "calls")
    before_requests = len(agent.llm.calls)
    agent.llm.decisions = [{"done": True, "summary": "ok"}]
    assert agent.run_task("主人任务")["status"] == "verified"
    requests = len(agent.llm.calls) - before_requests
    assert before_calls - agent.kernel.pools.remaining("global", "calls") == requests


def test_self_goal_auxiliary_calls_preserve_owner_pool(agent):
    node = agent.goal_tree.add("实现本地报告工具", "mid", source="self", acceptance="存在报告")
    before = agent.kernel.pools.remaining("owner_tasks", "tokens")
    agent.llm.decisions = [{"done": True, "summary": "ok"}]
    assert agent.work_on_goal(node)["status"] == "verified"
    assert agent.kernel.pools.remaining("owner_tasks", "tokens") == before


@pytest.mark.parametrize("operation", ["draft", "predict", "review", "brief", "proposal"])
def test_autonomous_housekeeping_preserves_owner_pool(agent, monkeypatch, operation):
    monkeypatch.setattr(agent.webtools, "github_search_repos", lambda topic: [{
        "full_name": "owner/repo", "stars": 1, "description": "local fixture"}])
    monkeypatch.setattr(agent.webtools, "github_readme", lambda repo: {
        "ok": True, "text": "local material"})
    before = agent.kernel.pools.remaining("owner_tasks", "tokens")
    calls_before = len(agent.llm.calls)
    if operation == "draft":
        agent._compose_draft()
    elif operation == "predict":
        agent._record_predictions("topic")
    elif operation == "review":
        agent.world.record_prediction("topic", "statement", deadline_ts=0.0, confidence=0.5)
        agent._review_predictions()
    elif operation == "brief":
        agent._morning_brief()
    else:
        from proto.exec.proposal_writing import rewrite_proposal
        rewrite_proposal(agent, "自主提议")
    assert len(agent.llm.calls) > calls_before
    assert agent.kernel.pools.remaining("owner_tasks", "tokens") == before


@pytest.mark.parametrize("self_initiated", [True, False])
def test_timeout_risk_assessment_uses_proposal_source_pool(agent, self_initiated):
    agent.queue.auto_quota = None
    agent.tools.register("external", lambda: "no effect", category=ActionCategory.VISIBLE_WRITE)
    agent.queue.submit(ActionProposal(
        ActionCategory.VISIBLE_WRITE, "风险评估", {"step": {"tool": "external"}},
        task_id="timeout", self_initiated=self_initiated))
    agent.owner_timeout_s = 0
    before = agent.kernel.pools.remaining("owner_tasks", "tokens")
    agent._maybe_timeout_decide()
    after = agent.kernel.pools.remaining("owner_tasks", "tokens")
    assert (after == before) if self_initiated else (after < before)


def test_loop_observes_actual_result_after_parameter_correction(agent):
    agent.llm.decisions = [
        {"tool": "clock", "args": {"noise": "ignored"}},
        {"tool": "clock", "args": {}},
        {"done": True, "summary": "ok"}]
    result = AgenticLoop(agent).run("观察真实结果")
    assert any("未知参数" in observation for observation in result["observations"])
    assert ":" in result["trace"][0]["result"]
    assert "忽略未知参数" not in result["trace"][0]["result"]


def test_goal_decomposition_is_budgeted(agent):
    before = agent.kernel.pools.remaining("global", "calls")
    agent.set_priority("开发本地计算工具")
    assert before - agent.kernel.pools.remaining("global", "calls") == 1


def test_same_clock_tasks_have_distinct_checkpoint_ids(agent):
    agent._now = lambda: 1234.0
    first = agent.run_task_with_steps("一", [{"tool": "clock", "args": {}}])
    second = agent.run_task_with_steps("二", [{"tool": "clock", "args": {}}])
    assert first["task_id"] != second["task_id"]


@pytest.mark.parametrize("amount", [-1, float("nan"), float("inf"), -float("inf"),
                                  True, None, "invalid"])
def test_invalid_consumption_does_not_modify_balances(amount):
    pools = BudgetPools({"global": {"tokens": 100}, "owner_tasks": {"tokens": 80}})
    before = pools.remaining_all()
    assert not pools.try_consume("owner_tasks", {"tokens": amount}).granted
    assert pools.remaining_all() == before


@pytest.mark.parametrize("amount", ["inf", "Infinity", "1e999", "-inf", "NaN"])
def test_non_finite_config_amount_is_rejected(amount):
    assert parse_amount(amount) is None


@pytest.mark.parametrize("amount", [-1, float("nan"), float("inf")])
def test_invalid_cap_is_rejected(amount):
    pools = BudgetPools({"global": {"tokens": 100}})
    with pytest.raises(ValueError):
        pools.set_cap("global", "tokens", amount)
    assert pools.remaining("global", "tokens") == 100


def test_budget_config_is_not_mutated_by_caps():
    config = {"global": {"tokens": 100}}
    pools = BudgetPools(config)
    pools.set_cap("global", "tokens", 200)
    assert config["global"]["tokens"] == 100


def test_refund_is_bounded_by_cap():
    pools = BudgetPools({"global": {"tokens": 100}, "owner_tasks": {"tokens": 80}})
    pools.refund("owner_tasks", {"tokens": 1000})
    assert pools.remaining_all() == {"global": {"tokens": 100}, "owner_tasks": {"tokens": 80}}


def test_refund_cannot_use_negative_amounts():
    pools = BudgetPools({"global": {"tokens": 100}})
    with pytest.raises(ValueError):
        pools.refund("global", {"tokens": -10})
    assert pools.remaining("global", "tokens") == 100


def test_actual_global_overrun_is_durable_and_trips_breaker(tmp_path):
    store = FileSystemStore(tmp_path)
    config = {"global": {"tokens": 100}, "owner_tasks": {"tokens": 80}}
    pools = BudgetPools(config, store=store)
    pools.try_consume("owner_tasks", {"tokens": 10})
    pools.settle("owner_tasks", {"tokens": 10}, {"tokens": 130})
    restored = BudgetPools(config, store=store)
    assert restored.remaining("global", "tokens") == -30
    assert restored.remaining("owner_tasks", "tokens") == -50
    assert restored.breaker_tripped is True
    assert not restored.try_consume("owner_tasks", {"tokens": 1}).granted


def test_saved_caps_apply_to_remaining_after_new_day(tmp_path):
    store = FileSystemStore(tmp_path)
    store.put("kernel_budget", "main", {
        "cycle_date": "2000-01-01", "caps": {"global": {"tokens": 500}},
        "remaining": {"global": {"tokens": 1}}})
    pools = BudgetPools({"global": {"tokens": 100}}, store=store)
    assert pools.remaining("global", "tokens") == 500


def test_reading_budget_rolls_over_cycle():
    pools = BudgetPools({"global": {"tokens": 100}})
    pools.try_consume("global", {"tokens": 80})
    pools._cycle_date = "2000-01-01"
    assert pools.remaining_all()["global"]["tokens"] == 100


def test_actual_usage_is_recorded_even_when_pool_is_exhausted(agent):
    class UsageLLM:
        last_usage = {"input_tokens": 50, "output_tokens": 250}

        def complete(self, system, user):
            return "ok"

    agent.kernel.pools.set_cap("owner_tasks", "tokens", 100)
    before = agent.kernel.pools.remaining("global", "tokens")
    assert agent._complete(UsageLLM(), "s", "u") == "ok"
    assert agent.kernel.pools.remaining("owner_tasks", "tokens") == -200
    assert before - agent.kernel.pools.remaining("global", "tokens") == 300


def test_estimated_input_overcharge_is_refunded(agent):
    class UsageLLM:
        last_usage = {"input_tokens": 2, "output_tokens": 3}

        def complete(self, system, user):
            return "ok"

    before = agent.kernel.pools.remaining("owner_tasks", "tokens")
    agent._complete(UsageLLM(), "s" * 400, "u")
    assert before - agent.kernel.pools.remaining("owner_tasks", "tokens") == 5


def test_refill_releases_budget_breaker():
    pools = BudgetPools({"global": {"tokens": 100}})
    assert not pools.try_consume("global", {"tokens": 101}).granted
    pools.refill()
    assert pools.try_consume("global", {"tokens": 1}).granted


def test_cap_batch_is_atomic_on_invalid_pool():
    pools = BudgetPools({"global": {"tokens": 100}, "owner_tasks": {"tokens": 80}})
    before = pools.remaining_all()
    with pytest.raises(KeyError):
        pools.set_caps({"owner_tasks": {"tokens": 40}, "ghost": {"tokens": 10}})
    assert pools.remaining_all() == before


@pytest.mark.parametrize("value", [None, [], {}, 42, True])
def test_acceptance_requires_string(value):
    class InvalidAcceptance:
        def complete(self, system, user):
            return json.dumps({"acceptance": value})

    assert generate_acceptance(InvalidAcceptance(), "任务") == ""


@pytest.mark.parametrize("missing", [["仍缺证据"], "不是数组", {}, None])
def test_contradictory_or_invalid_verdict_does_not_pass(missing):
    class InvalidVerdict:
        def complete(self, system, user):
            return json.dumps({"verdict": "pass", "missing": missing})

    verdict = verify("任务", "证据", [], "", InvalidVerdict())
    assert verdict.passed is False


def test_switching_workspace_rebinds_all_file_tools(agent, tmp_path):
    old = agent.code_workspace.root
    new = agent.set_projects_root(str(tmp_path / "new-projects"))
    agent.tools.get("write_file")("registered.txt", "new")
    result = agent.router.run(Task("new-root", "新路径", [{
        "tool": "write_file", "args": {"path": "router.txt", "content": "new"}}]))
    assert result.status == "completed"
    assert not (old / "registered.txt").exists()
    assert not (old / "router.txt").exists()
    assert agent.tools.get("read_file")("registered.txt")
    assert str(agent.router._workspace.root) == new


def test_switching_workspace_rebinds_command_tool(agent, tmp_path, monkeypatch):
    def command(workspace, argv, cwd="", timeout_s=120):
        return {"root": str(workspace.root)}

    monkeypatch.setattr(CodeTools, "run_command", command)
    new = agent.set_projects_root(str(tmp_path / "new-projects"))
    assert agent.tools.get("run_command")(["python3"])["root"] == new


def test_grep_cannot_read_outside_symlink(tmp_path):
    tools = CodeTools(tmp_path / "workspace")
    secret = tmp_path / "owner.key"
    secret.write_text("secret needle")
    (tools.root / "link.txt").symlink_to(secret)
    assert tools.grep("needle") == []


def test_grep_can_read_symlink_inside_workspace(tmp_path):
    tools = CodeTools(tmp_path / "workspace")
    tools.write_file("real.txt", "safe needle")
    (tools.root / "link.txt").symlink_to(tools.root / "real.txt")
    assert any(hit["path"] == "link.txt" for hit in tools.grep("needle"))


def test_file_paging_reaches_beyond_first_character_window(tmp_path):
    tools = CodeTools(tmp_path / "workspace")
    tools.write_file("large.txt", "\n".join(f"line-{number}:" + "x" * 100
                                             for number in range(1000)))
    page = tools.read_file("large.txt", offset=950, limit=3)
    assert "line-950:" in page and "line-952:" in page
    assert "共 1000 行" in page


def test_goal_completion_is_idempotent_and_notifies_parent_once(tmp_path):
    goals = GoalTree(FileSystemStore(tmp_path))
    parent = goals.add("整体", "long")
    child = goals.add("阶段", "mid", parent_id=parent.id)
    milestones = []
    goals.on_milestone = lambda node: milestones.append(node.id)
    goals.complete(child.id)
    goals.complete(child.id)
    assert milestones == [child.id, parent.id]


def test_dropped_goal_cannot_be_revived(tmp_path):
    goals = GoalTree(FileSystemStore(tmp_path))
    node = goals.add("丢弃", "mid")
    goals.drop(node.id)
    goals.complete(node.id)
    assert goals.get(node.id).status == "dropped"


def test_completed_parent_keeps_terminal_progress_when_child_is_dropped(tmp_path):
    goals = GoalTree(FileSystemStore(tmp_path))
    parent = goals.add("整体", "long")
    child = goals.add("阶段", "mid", parent_id=parent.id)
    goals.complete(child.id)
    goals.drop(child.id)
    assert goals.get(parent.id).status == "done"
    assert goals.get(parent.id).progress == 1.0


def test_goal_decomposition_skips_invalid_rows_and_is_idempotent(tmp_path):
    goals = GoalTree(FileSystemStore(tmp_path))
    node = goals.add("任务", "long")
    llm = AuditLLM()
    llm.children = [None, "invalid", {"text": None},
                    {"text": "有效阶段", "acceptance": None}]
    first = goals.decompose(node, llm)
    second = goals.decompose(node, llm)
    assert [child.id for child in first] == [child.id for child in second]
    assert len(first) == 2
    assert first[0].text == "有效阶段"
    assert first[0].acceptance == ""


def test_dropped_during_decomposition_creates_no_children(tmp_path):
    goals = GoalTree(FileSystemStore(tmp_path))
    node = goals.add("任务", "long")

    class DroppingLLM:
        def complete(self, system, user):
            goals.drop(node.id)
            return '[{"text": "不能复活"}]'

    assert goals.decompose(node, DroppingLLM()) == []
    assert goals.all_active() == []


def test_goal_forge_does_not_duplicate_active_goal(agent):
    text = "实现本地自动化测试工具"
    agent.state.owner_priority = text
    agent.goal_tree.add(text, "long", source="self")
    agent.llm.forge_rows = [{"text": text, "rationale": agent.persona,
                            "feasibility": 1, "acceptance": "存在测试文件与报告"}]
    assert agent.goal_forge.maybe_create(agent, trigger="schedule") is None
    assert len(agent.goal_tree.active_long_term()) == 1


@pytest.fixture()
def web_server():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{self.server.server_port}/secret")
                self.end_headers()
                return
            if self.path == "/allowed-redirect":
                self.send_response(302)
                self.send_header("Location", "/allowed")
                self.end_headers()
                return
            data = b"x" * (1024 * 1024 + 1) if self.path == "/large" else b"secret"
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", requests
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_redirect_cannot_leave_allowlist(web_server):
    base, requests = web_server
    result = WebToolkit(allowlist=("127.0.0.1",)).fetch_url(base + "/redirect")
    assert result["ok"] is False
    assert requests == ["/redirect"]


def test_web_response_size_is_bounded(web_server):
    base, _ = web_server
    result = WebToolkit(allowlist=("127.0.0.1",)).fetch_url(base + "/large")
    assert result["ok"] is False
    assert "too_large" in result["error"]


def test_allowed_redirect_still_works(web_server):
    base, requests = web_server
    result = WebToolkit(allowlist=("127.0.0.1",)).fetch_url(base + "/allowed-redirect")
    assert result["ok"] is True
    assert requests == ["/allowed-redirect", "/allowed"]


@pytest.mark.parametrize("url", [
    "http://[broken", "https://github.com:bad/path", "https://user:pass@github.com/",
])
def test_invalid_web_url_is_rejected(url):
    result = WebToolkit(fetch=lambda url, accept: (200, "no")).fetch_url(url)
    assert result["ok"] is False


def test_unsuccessful_http_status_is_not_success():
    result = WebToolkit(fetch=lambda url, accept: (404, "not found")).fetch_url(
        "https://github.com/missing")
    assert result["ok"] is False
