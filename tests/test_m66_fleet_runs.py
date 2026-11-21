import json
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date

import pytest

from proto.exec.contracts import object_schema
from proto.exec.router import Task, VARIANT_ID
from proto.kernel import budgets
from proto.kernel.kernel import ActionCategory
from proto.llm.client import LLMClient, LLMError
from proto.runtime.coordination import CoordinationError
from proto.runtime.manager import AgentManager
from proto.runtime.runcontext import current_run
from proto.runtime.runs import RunLease
from proto.web.server import ProtoWebServer


@contextmanager
def console(manager, **options):
    server = ProtoWebServer(manager=manager, port=0, token="offline-test", heartbeat_s=0, **options)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=4)


def request(server, path, body=None, *, key=None, token="offline-test"):
    headers = {"X-Proto-Token": token}
    if key is not None:
        headers["Idempotency-Key"] = key
    if body is not None:
        headers["Content-Type"] = "application/json"
    target = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", headers=headers,
                                    data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(target, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def members(root, model=None):
    fleet = AgentManager(root, llm=model)
    first_id = fleet.create("工程师")["id"]
    second_id = fleet.create("评审员")["id"]
    return fleet, fleet.get(first_id), fleet.get(second_id)


def enqueue(fleet, agent, text):
    return fleet.runs.enqueue(agent.fleet_id, "task", {"text": text})


def waited(fleet, run, expected):
    result = fleet.runs.wait(run["id"], timeout=4.0)
    assert result["status"] == expected, result
    return result


def test_fair_round_robin_and_no_tick_duplicates_after_restart(tmp_path):
    fleet, first, second = members(tmp_path)
    first, second = sorted((first, second), key=lambda agent: agent.fleet_id)
    first_id, second_id = first.fleet_id, second.fleet_id
    calls = []
    omitted = fleet.runs.enqueue(first_id, "tick")
    assert fleet.runs.enqueue(first_id, "tick")["id"] == omitted["id"]
    first_run, second_run = [enqueue(fleet, first, text) for text in ("first", "later")]
    other = fleet.runs.enqueue(second_id, "tick")
    restarted = AgentManager(tmp_path)
    restarted.get(first_id).run_task = lambda text: calls.append(text) or {"status": "completed"}
    restarted.get(first_id).maybe_proactive = lambda: calls.append("first-heartbeat")
    restarted.get(second_id).maybe_proactive = lambda: calls.append("other-heartbeat")
    restarted.runs.start(1)
    try:
        for run in (omitted, first_run, second_run, other):
            waited(restarted, run, "succeeded")
        assert calls == ["first", "other-heartbeat", "later", "first-heartbeat"]
    finally:
        restarted.runs.stop()


def test_blocked_writer_does_not_delay_other_agent_or_heartbeat(tmp_path):
    fleet, slow, free = members(tmp_path)
    for agent in (slow, free):
        agent.configure_autonomy(enabled=True, mode="auto")
    entered, release, free_tick = threading.Event(), threading.Event(), threading.Event()
    calls = []

    def long_task(text):
        assert not fleet.ops_lock(slow.fleet_id).acquire(blocking=False)
        calls.append(text)
        if text == "slow":
            entered.set()
            assert release.wait(4)
        return {"status": "completed"}

    slow.run_task = long_task
    slow.maybe_proactive = lambda: calls.append("slow-tick")
    free.maybe_proactive = lambda: free_tick.set()
    fleet.runs.start()
    try:
        slow_run = enqueue(fleet, slow, "slow")
        assert entered.wait(3)
        second = enqueue(fleet, slow, "serial")
        fleet.enqueue_ticks()
        assert free_tick.wait(3)
        assert fleet.runs.get(second["id"])["status"] == "queued"
        assert calls == ["slow"]
        with pytest.raises(CoordinationError, match="agent_busy"):
            fleet.archive(slow.fleet_id)
        release.set()
        waited(fleet, slow_run, "succeeded")
        waited(fleet, second, "succeeded")
    finally:
        release.set()
        fleet.runs.stop()


def test_queue_is_bounded_and_new_request_key_is_atomic(tmp_path):
    fleet, agent, _ = members(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as executor:
        submitted = list(executor.map(lambda _: fleet.runs.enqueue(agent.fleet_id, "task", {"text": "same"},
                                                                  key="owner-request"), range(16)))
    assert len({run["id"] for run in submitted}) == 1
    with pytest.raises(CoordinationError, match="idempotency_conflict"):
        fleet.runs.enqueue(agent.fleet_id, "task", {"text": "different"}, key="owner-request")
    for index in range(255):
        enqueue(fleet, agent, str(index))
    with pytest.raises(CoordinationError, match="run_queue_full"):
        enqueue(fleet, agent, "overflow")
    queued = submitted[0]
    assert fleet.runs.cancel(queued["id"])["status"] == "cancelled"
    assert AgentManager(tmp_path).runs.get(queued["id"])["status"] == "cancelled"
    enqueue(fleet, agent, "new-slot")


def test_one_live_dispatcher_per_fleet(tmp_path):
    fleet, _, _ = members(tmp_path)
    other = AgentManager(tmp_path)
    fleet.runs.start()
    try:
        for duplicate in (fleet, other):
            with pytest.raises(CoordinationError, match="already_running"):
                duplicate.runs.start()
    finally:
        fleet.runs.stop()
    other.runs.start()
    other.runs.stop()


def test_autonomous_selection_skips_goals_with_live_sessions(tmp_path):
    fleet, agent, _ = members(tmp_path)
    agent.configure_autonomy(enabled=True, mode="auto")
    parent = agent.goal_tree.add("已等待发布的专案", level="long")
    parked = agent.goal_tree.add("已等待发布的专案", level="task", parent_id=parent.id)
    available = agent.goal_tree.add("另一个可做的任务", level="short")
    agent.state.owner_priority = parent.text
    agent.sessions.save({"id": "parked-session", "status": "awaiting_approval", "context": {"goal_id": parked.id}})
    agent.scheduler.tick = lambda: set()
    agent._morning_brief = lambda: None
    claimed = []
    agent.work_on_goal = lambda node: claimed.append(node.id) or {"status": "completed"}
    fleet.runs.start(1)
    try:
        pulse = fleet.runs.enqueue(agent.fleet_id, "tick")
        waited(fleet, pulse, "succeeded")
        assert claimed == [available.id]
        assert agent.sessions.lookup("parked-session")["status"] == "awaiting_approval"
    finally:
        fleet.runs.stop()


def external(agent, effects):
    def effect(value):
        effects.append(value)
        return f"recorded:{value}"

    agent.tools.register("external_record", effect, category=ActionCategory.VISIBLE_WRITE, owner_only=True,
                         parameters=object_schema({"value": {"type": "string"}}, ["value"]))


_EXTERNAL_STEP = {"tool": "external_record", "args": {"value": "only once"}}


def test_waiting_approvals_release_slot_and_decisions_do_not_repeat(tmp_path):
    fleet, pending_agent, other = members(tmp_path)
    effects = []
    external(pending_agent, effects)
    pending_agent.run_task = lambda description: pending_agent.run_task_with_steps(description, [_EXTERNAL_STEP])
    other.run_task = lambda text: {"status": "completed", "summary": text}
    fleet.runs.start(1)
    try:
        first = waited(fleet, enqueue(fleet, pending_agent, "hold"), "awaiting_approval")
        assert first["waiting"]["task_id"] == first["result"]["task_id"]
        assert waited(fleet, enqueue(fleet, other, "another"), "succeeded")["result"]["summary"] == "another"
        token = first["generation"]
        for _ in range(2):
            assert fleet.runs.decide(first["id"], True, token)["id"] == first["id"]
        resumed = waited(fleet, first, "succeeded")
        assert resumed["generation"] == token + 1
        assert effects == ["only once"]
        second = waited(fleet, enqueue(fleet, pending_agent, "cancel it"), "awaiting_approval")
        fleet.runs.cancel(second["id"])
        waited(fleet, second, "cancelled")
        assert pending_agent.queue.pending() == []
        assert effects == ["only once"]
    finally:
        fleet.runs.stop()


class ScriptedTask(LLMClient):
    def __init__(self, decisions):
        self.decisions = iter(decisions)

    def complete(self, system, user):
        if "任务执行循环" in system:
            return json.dumps(next(self.decisions), ensure_ascii=False)
        return '{"acceptance":"有证据", "verdict":"pass", "missing":[]}'


def test_cancel_during_model_call_does_not_launch_returned_tool(tmp_path):
    start, release = threading.Event(), threading.Event()

    class BlockedModel(ScriptedTask):
        def complete(self, system, user):
            if "任务执行循环" in system:
                start.set()
                assert release.wait(4)
            return super().complete(system, user)

    fleet, agent, _ = members(tmp_path, BlockedModel([{"tool": "write_file", "args": {"path": "dropped.txt", "content": "bad"}}]))
    agent.llm_fast = None
    fleet.runs.start(1)
    try:
        run = enqueue(fleet, agent, "cancel before tool")
        assert start.wait(3)
        assert fleet.runs.cancel(run["id"])["cancel_requested"]
        release.set()
        cancelled = waited(fleet, run, "cancelled")
        assert cancelled["reason"] == "run_cancelled"
        assert not (agent.code_workspace.root / "dropped.txt").exists()
        assert fleet.runs.budget.for_run(run["id"])[0]["state"] == "settled"
    finally:
        release.set()
        fleet.runs.stop()


@pytest.mark.parametrize("crash", ["waiting", "confirmed_checkpoint", "final_session", "unknown"])
def test_restart_reconciles_receipts_without_replaying_effects(tmp_path, crash):
    original = ScriptedTask([{"done": True, "summary": "saved"}] if crash == "final_session" else [_EXTERNAL_STEP])
    fleet, agent, _ = members(tmp_path, original)
    agent.llm_fast = None
    effects = []
    external(agent, effects)
    enqueued = enqueue(fleet, agent, "simulate interrupted dispatch")
    claimed, _, lock = fleet.runs._pick()
    lease = RunLease(fleet.runs, claimed, agent)
    token = current_run.set(lease)
    try:
        result = agent.run_task("simulate interrupted dispatch")
        if crash == "confirmed_checkpoint":
            assert agent.router.resume(Task(result["task_id"], "original", []), True).status == "completed"
            assert effects == ["only once"]
        elif crash == "unknown":
            agent.checkpoints.put(result["task_id"], {"status": "resuming", "results": []}, variant_id=VARIANT_ID)
    finally:
        current_run.reset(token)
        lock.release()
    assert fleet.runs.get(enqueued["id"])["status"] == "running"

    model = ScriptedTask([{"done": True, "summary": "continued"}])
    restarted = AgentManager(tmp_path, llm=model)
    loaded = restarted.get(agent.fleet_id)
    loaded.llm_fast = None
    external(loaded, effects)
    restarted.runs.start()
    try:
        if crash == "unknown":
            uncertain = waited(restarted, enqueued, "needs_reconciliation")
            assert restarted.runs.find_link(loaded.fleet_id, result["task_id"])["id"] == uncertain["id"]
            with pytest.raises(CoordinationError, match="not_waiting"):
                restarted.runs.decide(uncertain["id"], True, uncertain["generation"])
            loaded.run_task = lambda text: {"status": "completed"}
            behind = enqueue(restarted, loaded, "after review")
            assert restarted.runs.blocked_reason(restarted.runs.get(behind["id"])) == "agent_needs_reconciliation"
            restarted.runs.stop()
            restarted.runs.start()
            assert restarted.runs.get(uncertain["id"])["status"] == "needs_reconciliation"
            restarted.runs.close_reconciliation(uncertain["id"], uncertain["generation"])
            waited(restarted, behind, "succeeded")
            assert effects == []
        elif crash == "final_session":
            assert waited(restarted, enqueued, "succeeded")["result"]["summary"] == "saved"
            assert effects == []
        else:
            parked = waited(restarted, enqueued, "awaiting_approval")
            expected = "awaiting_approval" if crash == "waiting" else crash
            assert parked["result"]["reason"] == expected
            assert len(effects) == (1 if crash == "confirmed_checkpoint" else 0)
            restarted.runs.decide(parked["id"], True, parked["generation"])
            assert waited(restarted, parked, "succeeded")["result"]["summary"] == "continued"
            assert effects == ["only once"]
    finally:
        restarted.runs.stop()


def test_both_budget_gates_and_no_cross_day_settlement(tmp_path, monkeypatch):
    days = [date(2026, 11, 21)]
    entered, release = threading.Event(), threading.Event()

    class Clock:
        @staticmethod
        def today():
            return days[0]

    class Model(LLMClient):
        def complete(self, system, user):
            if user == "hold":
                entered.set()
                assert release.wait(4)
            self.last_usage = {"input_tokens": 17, "output_tokens": 23}
            return "verified"

    monkeypatch.setattr(budgets, "date", Clock)
    fleet, first, second = members(tmp_path, Model())
    budget = fleet.runs.budget
    budget.day = lambda: days[0].isoformat()
    budget.configure(100, 1)
    self_before = first.kernel.pools.remaining("self_initiated", "tokens")
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(first._complete, first.llm, "fixed", "hold", "self_initiated")
        try:
            assert entered.wait(3)
            assert budget.view()["used"] == {"tokens": 2, "calls": 1}
            with pytest.raises(LLMError, match="fleet_budget_exhausted:calls"):
                second._complete(second.llm, "fixed", "cannot enter")
            days[0] = date(2026, 11, 22)
            assert first.kernel.pools.remaining("self_initiated", "tokens") == self_before
            assert second._complete(second.llm, "fixed", "new day") == "verified"
        finally:
            release.set()
        assert pending.result(timeout=4) == "verified"
    assert first.kernel.pools.remaining("self_initiated", "tokens") == self_before
    assert budget.view()["used"] == {"tokens": 40, "calls": 1}
    with fleet.runs.database.connect() as connection:
        receipts = connection.execute("SELECT cycle, pool, tokens, calls, state FROM model_tickets ORDER BY cycle").fetchall()
    assert [tuple(receipt) for receipt in receipts] == [
        ("2026-11-21", "self_initiated", 40, 1, "settled"),
        ("2026-11-22", "owner_tasks", 40, 1, "settled"),
    ]


def test_ticket_release_local_denial_actual_overrun_and_uncertain_receipt(tmp_path):
    fleet, agent, _ = members(tmp_path, ScriptedTask([]))
    budget = fleet.runs.budget
    budget.configure(30, 2)
    agent.kernel.pools.set_cap("self_initiated", "tokens", 0)
    with pytest.raises(LLMError, match="pool_shortfall:self_initiated"):
        agent._complete(agent.llm, "fixed", "local denial", "self_initiated")
    assert budget.view()["used"] == {"tokens": 0, "calls": 0}
    ticket = budget.reserve(agent.fleet_id, "owner_tasks", 10)
    assert budget.settle(ticket.id, tokens=40)
    assert not budget.settle(ticket.id, tokens=1)
    assert budget.view()["remaining"]["tokens"] == -10
    with pytest.raises(CoordinationError, match="fleet_budget_exhausted"):
        budget.reserve(agent.fleet_id, "self_initiated", 1)
    budget.configure(100, 5)

    class Failed(LLMClient):
        def complete(self, system, user):
            raise LLMError("provider timeout")

    with pytest.raises(LLMError, match="timeout"):
        agent._complete(Failed(), "fixed", "unknown")
    in_flight = budget.reserve(agent.fleet_id, "self_initiated", 13)
    assert budget.view()["uncertain"] == 1
    restarted = AgentManager(tmp_path)
    restarted.runs.start()
    try:
        observed = restarted.runs.budget.view()
        assert observed["used"] == {"tokens": 56, "calls": 3}
        assert observed["uncertain"] == 2
        assert not restarted.runs.budget.settle(in_flight.id, unsent=True)
    finally:
        restarted.runs.stop()


def test_web_fleet_budget_units_preserve_usage_and_agent_limits(tmp_path):
    fleet, agent, _ = members(tmp_path)
    path = "/api/fleet-budget"
    budget = fleet.runs.budget
    budget.day = lambda: "2026-11-21"
    agent_budget = agent.kernel.pools.remaining_all()
    with console(fleet) as server:
        settled = budget.reserve(agent.fleet_id, "owner_tasks", 12)
        assert budget.settle(settled.id, tokens=40)
        pending = budget.reserve(agent.fleet_id, "self_initiated", 20)
        status, view = request(server, path, {"tokens": "16万", "calls": "2k"})
        assert status == 200
        assert view["caps"] == {"tokens": 160000, "calls": 2000}
        assert view["used"] == {"tokens": 60, "calls": 2}
        assert view["remaining"] == {"tokens": 159940, "calls": 1998}
        assert view["in_flight"] == 1
        assert request(server, path, {"tokens": "1.2M", "calls": "0.0001k"}) == (
            400, {"error": "invalid_fleet_limits"})
        assert request(server, path) == (200, view)
        assert agent.kernel.pools.remaining_all() == agent_budget
        assert budget.settle(pending.id, tokens=25)

    reopened = AgentManager(tmp_path)
    reopened.runs.budget.day = budget.day
    with console(reopened) as server:
        _, persisted = request(server, path)
        assert persisted["caps"] == view["caps"]
        assert persisted["used"] == {"tokens": 65, "calls": 2}
        status, stopped = request(server, path, {"tokens": "1.2M", "calls": "0"})
        assert status == 200 and stopped["caps"] == {"tokens": 1200000, "calls": 0}
        with pytest.raises(CoordinationError, match="fleet_budget_exhausted:calls"):
            reopened.runs.budget.reserve(agent.fleet_id, "owner_tasks", 1)


def test_web_legacy_and_durable_jobs_are_authenticated_and_deduplicated(tmp_path):
    fleet, agent, _ = members(tmp_path)
    agent.run_task = lambda text: {"status": "completed", "summary": text}
    with console(fleet) as server:
        assert request(server, "/api/runs", token="invalid")[0] == 401
        assert request(server, "/api/fleet-budget", {"tokens": 25, "calls": True})[0] == 400
        assert request(server, "/api/fleet-budget", {"tokens": 25, "calls": 6})[1]["caps"] == {"tokens": 25, "calls": 6}
        status, first = request(server, "/api/task", {"text": "hello", "async": True}, key="http-repeat")
        assert status == 202
        status, existing = request(server, "/api/runs", {"kind": "task", "text": "hello"}, key="http-repeat")
        assert status == 202 and existing["id"] == first["run"]
        assert request(server, "/api/task", {"text": "changed"}, key="http-repeat")[0] == 409
        status, legacy = request(server, f"/api/agents/{agent.fleet_id}/act", {})
        assert status == 200
        waited(fleet, {"id": legacy["job"]}, "blocked")
        assert request(server, f"/api/jobs/{legacy['job']}")[1]["status"] == "done"
    restarted = AgentManager(tmp_path)
    with console(restarted) as server:
        assert request(server, f"/api/runs/{first['run']}")[1]["result"]["summary"] == "hello"
        assert request(server, f"/api/jobs/{legacy['job']}")[1]["result"]["reason_code"] == "not_enabled"
