import hashlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from proto.exec.context import ContextError, create_context, load_instructions, render_context
from proto.exec.plan_evidence import EvidenceError, OutputEvidence
from proto.llm.client import LLMClient
from proto.coordination.coordination import CoordinationError, encode
from proto.fleet.manager import AgentManager
from tests.test_m63_execution_sessions import decision, register_external
from tests.test_m66_fleet_runs import console, members, request, waited


class PlanModel(LLMClient):
    def __init__(self, decisions=(), *, proposal=None, verdict="pass"):
        self.decisions = iter(decisions)
        self.proposal = proposal
        self.verdict = verdict
        self.calls = []

    def complete(self, system, user):
        self.last_usage = {"input_tokens": 40, "output_tokens": 15}
        if system.startswith("你负责为主人指派"):
            self.calls.append(("plan", user))
            if callable(self.proposal):
                return self.proposal()
            if self.proposal is not None:
                return self.proposal
            result = {"steps": ["执行原任务", "核对文件和验收"], "acceptance": "检查原任务明确要求的内容"}
        elif system.startswith("你是 PROTO 的任务执行循环"):
            self.calls.append(("actor", user))
            result = next(self.decisions, {"done": True, "summary": "声称已完成"})
        elif system.startswith("你是独立验收员"):
            self.calls.append(("verify", user))
            result = {"verdict": self.verdict, "missing": [] if self.verdict == "pass" else ["内容不达标"]}
        else:
            self.calls.append(("unexpected", user))
            raise AssertionError(system)
        return json.dumps(result, ensure_ascii=False)


def recorded(fleet, response, status):
    return waited(fleet, {"id": response["run"]}, status)


def ready_plan(fleet, server, **fields):
    status, response = request(server, "/api/task/plan", {"text": "生成交付证明", **fields})
    assert status == 202, response
    return recorded(fleet, response, "succeeded")


def approve(server, plan, **options):
    route = f"/api/task/plan/{plan['id']}/approve"
    return request(server, route, {"sha256": plan["result"]["sha256"]}, **options)


def call_kinds(model):
    return [kind for kind, _ in model.calls]


def test_override_priority_is_directory_scoped_and_pinned_in_session(tmp_path):
    _, agent, _ = members(tmp_path)
    root = agent.code_workspace.root
    (root / "AGENTS.md").write_text("root default")
    project, sibling = root / "app", root / "sibling"
    project.mkdir()
    sibling.mkdir()
    (project / "AGENTS.override.md").write_text("app override")
    (project / "AGENTS.md").write_text("app default")
    (sibling / "AGENTS.override.md").write_text(" \n")
    (sibling / "AGENTS.md").write_text("sibling fallback")
    context = create_context(agent, "build", "", "owner_tasks", project_dir="app")
    assert context["active_instructions"] == ["AGENTS.md", "app/AGENTS.override.md"]
    assert "app override" in render_context(context)
    assert "app default" not in render_context(context)
    (root / "AGENTS.override.md").write_text("root override")
    assert load_instructions(context, "sibling", directory=True)
    assert "root override" not in render_context(context)
    assert "AGENTS.override.md" not in context["instructions"]
    fresh = create_context(agent, "build", "", "owner_tasks", project_dir="sibling")
    assert fresh["active_instructions"] == ["AGENTS.override.md", "sibling/AGENTS.md"]


def test_invalid_override_does_not_fall_back_to_default(tmp_path):
    _, agent, _ = members(tmp_path)
    root = agent.code_workspace.root
    (root / "AGENTS.md").write_text("valid fallback")
    (root / "AGENTS.override.md").symlink_to(root / "AGENTS.md")
    with pytest.raises(ContextError, match="instruction_symlink_rejected:AGENTS.override.md"):
        create_context(agent, "check", "", "owner_tasks")


def test_read_only_plan_and_concurrent_approval_execute_original_agent_once(tmp_path):
    model = PlanModel([decision("write_file", path="demo/report.txt", content="new evidence")])
    fleet, agent, other = members(tmp_path, model)
    fleet.set_current(agent.fleet_id)
    root = agent.code_workspace.root
    project = root / "demo"
    project.mkdir()
    (root / "AGENTS.md").write_text("root rules")
    (project / "AGENTS.override.md").write_text("project review rules")
    report = project / "report.txt"
    report.write_text("old evidence")
    with console(fleet) as server:
        assert request(server, "/api/task/plan", {"text": "unauthorized"}, token="invalid")[0] == 401
        before = agent.kernel.pools.remaining("owner_tasks", "tokens")
        ready = ready_plan(fleet, server, project_dir="demo", outputs=["report.txt"])
        snapshot = ready["result"]["plan"]
        assert report.read_text() == "old evidence"
        assert ready["session_id"] is None
        assert call_kinds(model) == ["plan"]
        assert "old evidence" not in model.calls[0][1]
        assert agent.kernel.pools.remaining("owner_tasks", "tokens") == before - 55
        assert [rule["path"] for rule in snapshot["rules"]] == ["AGENTS.md", "demo/AGENTS.override.md"]
        assert snapshot["outputs"] == [{"path": "report.txt", "before_sha256": hashlib.sha256(b"old evidence").hexdigest()}]
        assert ready["result"]["sha256"] == hashlib.sha256(encode(snapshot).encode()).hexdigest()
        assert request(server, f"/api/runs/{ready['id']}")[1]["budget"][0]["pool"] == "owner_tasks"
        assert request(server, "/api/runs?reviewable=true")[1]["runs"][0]["id"] == ready["id"]
        assert approve(server, ready, token="invalid")[0] == 401
        route = f"/api/task/plan/{ready['id']}/approve"
        assert request(server, route, {})[0] == 400
        assert request(server, route, {"sha256": "0" * 64})[0] == 409
        assert request(server, "/api/runs", {"kind": "planned_task"})[0] == 400
        assert request(server, "/api/task", {"text": "can't squat key"}, key="plan-exec:" + ready["id"])[0] == 400
        assert [run["kind"] for run in fleet.runs.list()] == ["plan"]
        fleet.set_current(other.fleet_id)
        with ThreadPoolExecutor(max_workers=4) as executor:
            approvals = list(executor.map(lambda _: approve(server, ready), range(4)))
        assert all(status == 202 for status, _ in approvals), approvals
        assert len({payload["run"] for _, payload in approvals}) == 1
        execution = recorded(fleet, approvals[0][1], "succeeded")
        assert request(server, "/api/runs?reviewable=true")[1]["runs"] == []
    assert execution["agent_id"] == agent.fleet_id
    assert execution["result"]["status"] == "verified"
    assert execution["result"]["plan_id"] == ready["id"]
    assert report.read_text() == "new evidence"
    assert execution["result"]["output_evidence"] == [{"path": "report.txt", "passed": True, "reason": "",
                                                       "sha256": hashlib.sha256(b"new evidence").hexdigest()}]
    assert call_kinds(model) == ["plan", "actor", "actor", "verify"]
    assert "检查原任务明确要求的内容" in model.calls[-1][1]
    assert "[主人批准的执行计划]" in model.calls[1][1]


def test_plan_survives_restart_and_tool_approval_is_separate(tmp_path):
    fleet, agent, _ = members(tmp_path, PlanModel())
    fleet.set_current(agent.fleet_id)
    with console(fleet) as server:
        ready = ready_plan(fleet, server, outputs=["proof.txt"])
    model = PlanModel([decision("external", value="once"),
                       decision("write_file", path="proof.txt", content="resumed proof")])
    restarted = AgentManager(tmp_path, llm=model)
    agent = restarted.get(agent.fleet_id)
    effects = []
    register_external(agent, effects)
    with console(restarted) as server:
        assert request(server, "/api/runs?reviewable=true")[1]["runs"][0]["id"] == ready["id"]
        status, queued = approve(server, ready)
        assert status == 202
        paused = recorded(restarted, queued, "awaiting_approval")
        assert effects == []
        assert not (agent.code_workspace.root / "proof.txt").exists()
        assert request(server, f"/api/runs/{paused['id']}/resume", {"approved": True,
                       "generation": paused["generation"]})[0] == 202
        completed = recorded(restarted, queued, "succeeded")
        assert completed["session_id"] == paused["session_id"]
        assert approve(server, ready)[1]["run"] == paused["id"]
    assert effects == ["once"]
    assert completed["result"]["output_evidence"][0]["passed"]
    assert call_kinds(model) == ["actor", "actor", "actor", "verify"]


@pytest.mark.parametrize("change,reason", [("override", "plan_rules_changed"), ("output", "plan_outputs_changed"),
                                            ("workspace", "workspace_changed")])
def test_drift_blocks_approved_execution_before_model(tmp_path, change, reason):
    model = PlanModel()
    fleet, agent, _ = members(tmp_path, model)
    fleet.set_current(agent.fleet_id)
    root = agent.code_workspace.root
    (root / "AGENTS.md").write_text("original rule")
    with console(fleet) as server:
        ready = ready_plan(fleet, server, outputs=["proof.txt"])
        if change == "override":
            (root / "AGENTS.override.md").write_text("changed scope")
        elif change == "output":
            (root / "proof.txt").write_text("changed before executing")
        else:
            agent.set_projects_root(str(tmp_path / "other-projects"))
        status, queued = approve(server, ready)
        assert status == 202
        blocked = recorded(fleet, queued, "blocked")
        assert blocked["reason"] == reason
        assert blocked["session_id"] is None
    assert call_kinds(model) == ["plan"]


@pytest.mark.parametrize("before,write,reason", [(None, None, "output_missing"), ("old", None, "output_unchanged"),
                                                  (None, " \n", "output_blank")])
def test_done_claim_cannot_bypass_file_gate(tmp_path, before, write, reason):
    model = PlanModel([decision("write_file", path="proof.txt", content=write)] if write is not None else [])
    fleet, agent, _ = members(tmp_path, model)
    fleet.set_current(agent.fleet_id)
    if before is not None:
        (agent.code_workspace.root / "proof.txt").write_text(before)
    with console(fleet) as server:
        ready = ready_plan(fleet, server, outputs=["proof.txt"])
        status, queued = approve(server, ready)
        assert status == 202
        failed = recorded(fleet, queued, "failed")["result"]
    assert failed["reason"] == "plan_evidence_failed"
    assert not failed["verified"]
    assert failed["output_evidence"][0]["reason"] == reason
    assert call_kinds(model) == ["plan", *(["actor"] * (1 if write is None else 2))]


@pytest.mark.parametrize("outputs", [[], ["proof.txt"]])
def test_hard_evidence_does_not_override_semantic_failure(tmp_path, outputs):
    model = PlanModel([decision("write_file", path="proof.txt", content="proof")] if outputs else [], verdict="fail")
    fleet, agent, _ = members(tmp_path, model)
    fleet.set_current(agent.fleet_id)
    with console(fleet) as server:
        ready = ready_plan(fleet, server, outputs=outputs)
        status, queued = approve(server, ready)
        assert status == 202
        failed = recorded(fleet, queued, "failed")["result"]
    assert failed["missing"] == ["内容不达标"]
    assert failed["verified"] is False
    assert len(failed["output_evidence"]) == len(outputs)
    assert all(receipt["passed"] for receipt in failed["output_evidence"])
    assert call_kinds(model).count("verify") == 1


@pytest.mark.parametrize("bad_reply", ['{"tool":"write_file","args":{}}',
                                       '{"steps":["one"],"acceptance":"a","acceptance":"b"}',
                                       '{"steps":[],"acceptance":"claim"}'])
def test_invalid_planner_output_cannot_be_approved(tmp_path, bad_reply):
    model = PlanModel(proposal=bad_reply)
    fleet, _, _ = members(tmp_path, model)
    with console(fleet) as server:
        status, queued = request(server, "/api/task/plan", {"text": "test"})
        assert status == 202
        failed = recorded(fleet, queued, "failed")
        assert failed["reason"] == "invalid_plan_response"
        assert request(server, f"/api/task/plan/{failed['id']}/approve", {"sha256": "0" * 64})[0] == 409
    assert call_kinds(model) == ["plan"]


@pytest.mark.parametrize("path", ["../proof.txt", ".GIT/config", "/tmp/proof.txt", "demo\\proof.txt"])
def test_out_of_scope_output_is_rejected_at_enqueue(tmp_path, path):
    fleet, agent, _ = members(tmp_path)
    with pytest.raises(CoordinationError, match="invalid_output_paths"):
        fleet.runs.task_plans.enqueue(agent.fleet_id, "task", "", [path])
    assert fleet.runs.list() == []


def test_output_reader_rejects_links_nonregular_and_oversize(tmp_path):
    _, agent, _ = members(tmp_path)
    root = agent.code_workspace.root
    (root / "proof.txt").write_text("local artifact")
    evidence = OutputEvidence(agent.code_workspace, "")
    snapshot = evidence.snapshot(["proof.txt"])
    (root / "proof.txt").unlink()
    external = tmp_path / "external.txt"
    external.write_text("not in workspace")
    (root / "proof.txt").symlink_to(external)
    assert evidence.verify(snapshot)[0]["reason"] == "output_unsafe_or_unreadable"
    (root / "linked").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(EvidenceError, match="output_unsafe_or_unreadable"):
        evidence.snapshot(["linked/external.txt"])
    os.mkfifo(root / "pipe")
    with pytest.raises(EvidenceError, match="output_not_regular"):
        evidence.snapshot(["pipe"])
    (root / "large.txt").write_bytes(b"x" * 16385)
    (root / "invalid.txt").write_bytes(b"\xff")
    for output, reason in [("large.txt", "output_too_large"), ("invalid.txt", "output_not_utf8_text")]:
        with pytest.raises(EvidenceError, match=reason):
            evidence.snapshot([output])


def test_plan_cancellation_during_model_call_never_leaves_approvable_plan(tmp_path):
    started, release = threading.Event(), threading.Event()

    def delayed_plan():
        started.set()
        assert release.wait(4)
        return '{"steps":["one"],"acceptance":"criterion"}'

    fleet, _, _ = members(tmp_path, PlanModel(proposal=delayed_plan))
    with console(fleet) as server:
        status, queued = request(server, "/api/task/plan", {"text": "cancel while planning"})
        try:
            assert status == 202 and started.wait(4)
            assert request(server, f"/api/runs/{queued['run']}/cancel", {})[0] == 200
        finally:
            release.set()
        cancelled = recorded(fleet, queued, "cancelled")
        assert request(server, f"/api/task/plan/{cancelled['id']}/approve", {"sha256": "0" * 64})[0] == 409
    assert "plan" not in cancelled["result"]


def test_interrupted_read_only_run_keeps_uncertain_usage_without_blocking_queue(tmp_path):
    fleet, agent, _ = members(tmp_path)
    plan = fleet.runs.task_plans.enqueue(agent.fleet_id, "test", "", [])
    claim, _, lock = fleet.runs._pick()
    lock.release()
    assert claim["id"] == plan["id"]
    fleet.runs.budget.reserve(agent.fleet_id, "owner_tasks", 30, plan["id"])
    later = fleet.runs.enqueue(agent.fleet_id, "tick")
    restarted = AgentManager(tmp_path)
    restarted.runs.recover()
    recovered = restarted.runs.get(plan["id"])
    assert (recovered["status"], recovered["reason"]) == ("failed", "plan_interrupted")
    assert restarted.runs.budget.for_run(plan["id"])[0]["state"] == "uncertain"
    assert restarted.runs.blocked_reason(restarted.runs.get(later["id"])) == ""


def test_plan_cannot_bypass_fleet_budget(tmp_path):
    model = PlanModel()
    fleet, _, _ = members(tmp_path, model)
    fleet.runs.budget.configure(0, 0)
    with console(fleet) as server:
        status, queued = request(server, "/api/task/plan", {"text": "test"})
        assert status == 202
        blocked = recorded(fleet, queued, "blocked")
    assert blocked["reason"].startswith("fleet_budget_exhausted:")
    assert call_kinds(model) == []
