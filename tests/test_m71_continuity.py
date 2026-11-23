import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from proto.exec.agentic import LOOP_SYSTEM, AgenticLoop
from proto.exec.work_context import MAX_ACTOR_BYTES, MAX_HISTORY_BYTES, render_history, size
from proto.fleet import runs as dispatcher
from proto.coordination.coordination import CoordinationError
from proto.fleet.manager import AgentManager
from proto.work.journal import GoalJournal
from tests.test_m66_fleet_runs import console, request, waited


TASK = "推进 SwiftUI 交付"
ACCEPTANCE = "Product.swift 已写入，包含产品名称"
WRITE = {"tool": "write_file", "args": {"path": "Product.swift", "content": "产品名称"}, "category": "workspace_write"}
DONE = {"done": True, "summary": "可交付"}
PASS = {"verdict": "pass"}
EXIT_AT_CHECKPOINT = 87


class Model:
    def __init__(self, decisions=(), verdicts=()):
        self.decisions = iter(decisions)
        self.verdicts = iter(verdicts)
        self.actor_inputs = []
        self.verifier_inputs = []

    def complete(self, system, user):
        if system == LOOP_SYSTEM:
            self.actor_inputs.append((system, user))
            reply = next(self.decisions)
        elif system.startswith("你是独立验收员"):
            self.verifier_inputs.append((system, user))
            reply = next(self.verdicts)
        else:
            raise AssertionError(system)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            reply = reply()
        return json.dumps(reply, ensure_ascii=False)


def member(root, model):
    fleet = AgentManager(root, llm=model)
    identifier = fleet.create("SwiftUI 开发专家")["id"]
    agent = fleet.get(identifier)
    agent.llm_fast = None
    agent.configure_autonomy(enabled=True, mode="auto")
    return fleet, agent


def start_autonomous(fleet, agent, goal=None):
    goal = goal or agent.goal_tree.add(TASK, "mid", acceptance=ACCEPTANCE)
    journal = GoalJournal(agent.data_dir / "work" / goal.id / "JOURNAL.md")
    agent.maybe_proactive = lambda: AgenticLoop(agent, max_steps=3, pool="self_initiated").run(
        TASK, acceptance=ACCEPTANCE, goal_id=goal.id, journal=journal)
    return fleet.runs.enqueue(agent.fleet_id, "tick"), goal


def step(fleet):
    run, agent, lock = fleet.runs._pick()
    try:
        fleet.runs._execute(run, agent)
    finally:
        lock.release()
    return fleet.runs.get(run["id"])


def record_writes(agent, counter, *, interrupt=False):
    actual_write = agent.code_workspace.write_file

    def write(path, content):
        result = actual_write(path, content)
        with counter.open("a", encoding="utf-8") as handle:
            handle.write("写入\n")
        if interrupt:
            os._exit(EXIT_AT_CHECKPOINT)
        return result

    agent.code_workspace.write_file = write


def run_until_crash(root, phase):
    decisions = [WRITE, (lambda: os._exit(EXIT_AT_CHECKPOINT)) if phase == "model" else DONE]
    fleet, agent = member(root, Model(decisions, [PASS]))
    run, goal = start_autonomous(fleet, agent)
    (root / "ids.json").write_text(json.dumps([run["id"], agent.fleet_id, goal.id]), encoding="utf-8")
    record_writes(agent, root / "effects.txt", interrupt=phase == "unknown")
    save, dispatch = agent.sessions.save, agent.router.run

    def save_or_crash(session):
        save(session)
        if ((phase == "decision" and session.get("pending_decision"))
                or (phase == "verified" and session["status"] == "verified")):
            os._exit(EXIT_AT_CHECKPOINT)

    def dispatch_or_crash(task):
        result = dispatch(task)
        if phase == "receipt" and result.status == "completed":
            os._exit(EXIT_AT_CHECKPOINT)
        return result

    agent.sessions.save = save_or_crash
    agent.router.run = dispatch_or_crash
    step(fleet)
    raise AssertionError(f"did not interrupt at {phase}")


@pytest.mark.parametrize("phase", ["model", "decision", "receipt", "unknown", "verified"])
def test_process_death_resumes_same_autonomous_run_without_replaying_tools(tmp_path, phase):
    child = subprocess.run(
        [sys.executable, "-c", "import runpy, sys; runpy.run_path(sys.argv.pop(1), run_name='__main__')",
         __file__, str(tmp_path), phase], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, timeout=15)
    assert child.returncode == EXIT_AT_CHECKPOINT, child.stdout + child.stderr
    run_id, agent_id, goal_id = json.loads((tmp_path / "ids.json").read_text(encoding="utf-8"))
    counter = tmp_path / "effects.txt"
    assert counter.exists() is (phase != "decision")
    model = Model([DONE], [PASS])
    restarted = AgentManager(tmp_path, llm=model)
    agent = restarted.get(agent_id)
    agent.llm_fast = None
    record_writes(agent, counter)
    interrupted = restarted.runs.get(run_id)
    assert interrupted["status"] == "running"
    session = agent.sessions.lookup(interrupted["session_id"])
    assert session["context"]["pool"] == "self_initiated"
    assert session["context"]["acceptance"] == ACCEPTANCE
    assert session["next_step"] == (2 if phase == "verified" else 1)
    assert agent.goal_tree.get(goal_id).status == "active"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(agent, "maybe_proactive", lambda: pytest.fail("must continue, not plan a new attempt"))
        restarted.runs.start(1)
        try:
            saved = restarted.runs.wait(run_id, timeout=5)
            assert saved["session_id"] == session["id"]
            assert counter.read_text(encoding="utf-8") == "写入\n"
            if phase == "unknown":
                assert saved["status"] == "needs_reconciliation"
                assert agent.goal_tree.get(goal_id).status == "active"
                assert goal_id in agent.sessions.pending_goal_ids()
                with pytest.raises(CoordinationError, match="unsafe_resume_checkpoint"):
                    restarted.runs.continue_reconciliation(run_id, saved["generation"])
                restarted.runs.close_reconciliation(run_id, saved["generation"])
                assert goal_id not in agent.sessions.pending_goal_ids()
                assert agent.sessions.lookup(session["id"])["pending_call"] == session["pending_call"]
            else:
                assert saved["status"] == "succeeded", saved
                assert saved["result"]["proposal"]["verified"] is True
                assert agent.goal_tree.get(goal_id).status == "done"
                trace = agent.sessions.lookup(session["id"])["trace"]
                assert len(trace) == 1 and trace[0]["result"] == "written:Product.swift"
                assert len({entry["step"] for entry in agent.sessions.lookup(session["id"])["events"]
                            if entry["kind"] == "decision"}) == 2
                if phase != "verified":
                    assert "已写入 Product.swift" in model.actor_inputs[0][1]
            assert len(model.actor_inputs) == (0 if phase in ("unknown", "verified") else 1)
        finally:
            restarted.runs.stop()


@pytest.mark.parametrize("stage", ["decision", "verification"])
def test_transient_model_failure_continues_only_missing_stage(tmp_path, monkeypatch, stage):
    monkeypatch.setattr(dispatcher, "_MODEL_RETRY_DELAYS", (0.0,))
    error = TimeoutError("offline fixture")
    model = Model([WRITE, error, DONE] if stage == "decision" else [WRITE, DONE],
                  [error, PASS] if stage == "verification" else [PASS])
    fleet, agent = member(tmp_path, model)
    initial, _ = start_autonomous(fleet, agent)
    counter = tmp_path / "effects.txt"
    record_writes(agent, counter)
    pending = step(fleet)
    assert pending["status"] == "queued" and pending["reason"] == "model_retry_wait"
    assert pending["result"]["model_reason"] == "model_timeout"
    assert fleet.runs.enqueue(agent.fleet_id, "tick")["id"] == initial["id"]
    saved = agent.sessions.lookup(pending["session_id"])
    assert saved["status"] == "waiting_model"
    assert (saved["pending_decision"] is not None) is (stage == "verification")
    resumed = step(fleet)
    assert resumed["id"] == pending["id"] and resumed["session_id"] == saved["id"]
    assert resumed["status"] == "succeeded" and resumed["result"]["model_retries"] == 1
    assert counter.read_text(encoding="utf-8") == "写入\n"
    assert len(model.actor_inputs) == (2 if stage == "verification" else 3)
    assert len(model.verifier_inputs) == (2 if stage == "verification" else 1)
    assert ACCEPTANCE not in model.verifier_inputs[-1][0]
    assert "产品名称" in model.verifier_inputs[-1][1]


@pytest.mark.parametrize("issue", ["limited", "auth"])
def test_manual_continue_requires_valid_safe_receipt_after_model_retries(tmp_path, monkeypatch, issue):
    monkeypatch.setattr(dispatcher, "_MODEL_RETRY_DELAYS", (0.0,))
    failure = TimeoutError("offline fixture")
    errors = [failure, failure] if issue == "limited" else [HTTPError("https://example.invalid", 401, "private", {}, None)]
    model = Model([WRITE, *errors, DONE], [PASS])
    fleet, agent = member(tmp_path, model)
    start_autonomous(fleet, agent)
    counter = tmp_path / "effects.txt"
    record_writes(agent, counter)
    parked = step(fleet)
    if issue == "limited":
        assert parked["status"] == "queued"
        parked = step(fleet)
    assert parked["status"] == "needs_reconciliation"
    assert parked["reason"] == ("model_retry_exhausted" if issue == "limited" else "safe_resume_model")
    assert fleet.runs._pick() is None
    assert "private" not in json.dumps(parked)
    with pytest.raises(CoordinationError, match="stale_generation"):
        fleet.runs.continue_reconciliation(parked["id"], parked["generation"] - 1)
    queued = fleet.runs.continue_reconciliation(parked["id"], parked["generation"])
    assert queued["status"] == "queued"
    final = step(fleet)
    assert final["status"] == "succeeded" and final["session_id"] == parked["session_id"]
    assert counter.read_text(encoding="utf-8") == "写入\n"


@pytest.mark.parametrize("stage", ["actor", "verifier"])
def test_shared_provider_cooldown_keeps_same_run_without_replaying_work(tmp_path, monkeypatch, stage):
    clock = [time.time()]

    def now():
        return clock[0]

    def reject_after_switch():
        model.pressure_key = "different-route"
        raise HTTPError("https://example.invalid", 429, "private", {"Retry-After": "90"}, None)

    monkeypatch.setattr(dispatcher, "time", SimpleNamespace(time=now, monotonic=time.monotonic))
    model = (Model([WRITE, reject_after_switch, DONE], [PASS]) if stage == "actor"
             else Model([WRITE, DONE], [reject_after_switch, PASS]))
    model.pressure_key = "test-route"
    fleet, agent = member(tmp_path, model)
    fleet.runs.model_pressure.now = now
    run, goal = start_autonomous(fleet, agent)
    counter = tmp_path / "effects.txt"
    record_writes(agent, counter)

    parked = step(fleet)
    assert parked["status"] == "queued" and parked["reason"] == "model_retry_wait"
    assert parked["result"]["model_reason"] == "model_rate_limited"
    assert parked["result"]["retry_at"] == clock[0] + 90
    assert goal.id in agent.sessions.pending_goal_ids()
    clock[0] += 89
    assert fleet.runs._pick() is None and len(model.actor_inputs) == 2

    restarted = AgentManager(tmp_path, llm=model)
    restarted.runs.model_pressure.now = now
    record_writes(restarted.get(agent.fleet_id), counter)
    assert restarted.runs._pick() is None
    clock[0] += 1
    final = step(restarted)
    assert final["status"] == "succeeded" and final["id"] == run["id"]
    assert final["session_id"] == parked["session_id"]
    assert counter.read_text(encoding="utf-8") == "写入\n"


def test_cancel_delayed_autonomous_resume_without_waiting_for_retry(tmp_path):
    fleet, agent = member(tmp_path, Model([WRITE, TimeoutError("offline fixture")]))
    run, goal = start_autonomous(fleet, agent)
    queued = step(fleet)
    assert queued["status"] == "queued" and fleet.runs._pick() is None
    assert goal.id in agent.sessions.pending_goal_ids()
    fleet.pause(agent.fleet_id)
    fleet.runs.cancel(run["id"])
    final = step(fleet)
    assert final["status"] == "cancelled"
    assert goal.id not in agent.sessions.pending_goal_ids()
    saved = agent.sessions.lookup(queued["session_id"])
    assert len(saved["trace"]) == 1 and saved["status"] == "cancelled"


def test_restart_requeue_is_bounded_even_if_process_repeatedly_dies(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatcher, "_MAX_RESTART_RESUMES", 2)

    def interrupt():
        raise KeyboardInterrupt

    model = Model([WRITE, interrupt, interrupt, interrupt, DONE], [PASS])
    fleet, agent = member(tmp_path, model)
    run, _ = start_autonomous(fleet, agent)
    counter = tmp_path / "effects.txt"
    record_writes(agent, counter)
    for attempt in range(3):
        with pytest.raises(KeyboardInterrupt):
            step(fleet)
        fleet.runs.recover()
        recovered = fleet.runs.get(run["id"])
        assert recovered["status"] == ("queued" if attempt < 2 else "needs_reconciliation")
    assert recovered["reason"] == "safe_resume_limit"
    assert recovered["result"]["restart_attempts"] == 2
    assert counter.read_text(encoding="utf-8") == "写入\n"


def test_console_safe_continue_requires_explicit_confirmation(tmp_path):
    model = Model([WRITE, DONE], [HTTPError("https://example.invalid", 403, "private", {}, None), PASS])
    fleet, agent = member(tmp_path, model)
    run, _ = start_autonomous(fleet, agent)
    with console(fleet, workers=1) as server:
        parked = waited(fleet, run, "needs_reconciliation")
        assert parked["reason"] == "safe_resume_model"
        path = f"/api/runs/{run['id']}/reconcile"
        generation = parked["generation"]
        assert request(server, path, {"generation": generation, "continue_safe": True})[0] == 400
        assert request(server, path, {"generation": generation, "confirmed": True, "continue_safe": "true"})[0] == 400
        assert request(server, path, {"generation": generation, "confirmed": True, "continue_safe": True})[0] == 202
        final = waited(fleet, run, "succeeded")
        assert final["session_id"] == parked["session_id"]
        assert len(model.actor_inputs) == 2 and len(model.verifier_inputs) == 2


def test_actor_context_is_bounded_and_keeps_older_receipts(tmp_path):
    unique = "不应再次发送的写入正文"
    write = {**WRITE, "args": {**WRITE["args"], "content": unique * 60}}
    large = ("大观察的中文文件" * 380 + "\n") * 8
    reads = [{"tool": "read_file", "args": {"path": f"notes-{offset}.txt"}}
             for offset in range(5)]
    model = Model([write, *reads, DONE])
    _, agent = member(tmp_path, model)
    for request in reads:
        agent.code_workspace.write_file(request["args"]["path"], large)
    result = AgenticLoop(agent).run(TASK)
    assert result["status"] == "completed"
    session = agent.sessions.lookup(result["session_id"])
    for system, user in model.actor_inputs:
        assert size(system + user) <= MAX_ACTOR_BYTES
        assert user.count(TASK) == 1
        assert unique not in user
        assert size(user[user.index("\n\n已有观察"):]) <= MAX_HISTORY_BYTES
    assert "已写入 Product.swift" in model.actor_inputs[-1][1]
    history = model.actor_inputs[-1][1].split("\n\n已有观察", 1)[1]
    assert "notes-4.txt" in history
    assert "notes-0.txt" not in history
    assert len(session["events"][3]["results"][0]) > 3000
    assert render_history([], [{"tool": "run_command", "args": {"argv": ["pytest"]},
                                 "result": {"exit": 1}}]).endswith("退出码 1\n")


def test_oversized_mandatory_context_fails_closed(tmp_path):
    _, agent = member(tmp_path, Model([WRITE]))
    result = AgenticLoop(agent).run("中" * MAX_ACTOR_BYTES)
    assert result["status"] == "failed" and result["reason"] == "context_budget_exceeded"
    assert not agent.llm.actor_inputs
    assert not (agent.code_workspace.root / "Product.swift").exists()


def test_journal_does_not_treat_failed_edit_or_model_summary_as_artifact(tmp_path):
    journal = GoalJournal(tmp_path / "JOURNAL.md")
    trace = [{"tool": "edit_file", "args": {"path": "no-change", "old": "a", "new": "b"}, "result": "0"},
             {"tool": "write_file", "args": {"path": "no-write"}, "result": "write_file 执行失败"}]
    journal.finish("max_steps", [], trace, summarize=lambda *_: "产出：并未写入.md\n下一步：校正")
    assert journal.latest_round()["produced"] == "无"
    assert journal.consecutive_no_output() == 1


if __name__ == "__main__":
    run_until_crash(Path(sys.argv[1]), sys.argv[2])
