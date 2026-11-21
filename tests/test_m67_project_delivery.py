import hashlib
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from proto.llm.client import LLMClient
from proto.runtime.coordination import CoordinationError, _SCHEMA
from proto.runtime.manager import AgentManager

from tests.test_m66_fleet_runs import console, members, request, waited


class DeliveryModel(LLMClient):
    def __init__(self, produce=None, review=None):
        self.log = []
        self.produce = produce or self._produce
        self.review = review or (lambda *_: {"accept": True, "note": "逐项核对了固定版本"})

    @staticmethod
    def _produce(system, data):
        approach = "验证优先" if "人设：证据工程师\n" in system else "最小化交付"
        return {"artifacts": {name: json.dumps({"proof": data["title"], "strategy": approach}, ensure_ascii=False)
                              for name in data["contract"]}}

    def complete(self, system, user):
        data = json.loads(user)
        reviewing = "【项目评审】" in system
        self.log.append(("review" if reviewing else "work", system, data))
        self.last_usage = {"input_tokens": 75, "output_tokens": 25}
        result = (self.review if reviewing else self.produce)(system, data)
        return json.dumps(result, ensure_ascii=False)


def specification(assignee, *, title="基础证明", reviewer=None, depends_on=(), **overrides):
    return {"title": title, "brief": "输出可核对的交付证据，不运行命令。", "assignee": assignee.fleet_id,
            "reviewer": reviewer.fleet_id if reviewer else None, "depends_on": list(depends_on),
            "outputs": [{"path": "proof.json", "json_keys": ["proof"], "contains": ["proof"]}], **overrides}


def work_view(fleet, item):
    return next(work for work in fleet.runs.projects.view(item["project_id"])["work"] if work["id"] == item["id"])


def settle_work(fleet, item, state):
    deadline = time.monotonic() + 4
    while True:
        current = work_view(fleet, item)
        if current["state"] == state:
            return current
        if time.monotonic() >= deadline or current["state"] in {"verified", "rework", "dropped", "needs_reconciliation"}:
            pytest.fail(f"unexpected delivery state: {current}")
        time.sleep(0.02)


def test_scoped_handoff_waits_for_independent_review_and_survives_restart(tmp_path):
    model = DeliveryModel()
    fleet, producer, reviewer = members(tmp_path, model)
    producer.persona = "证据工程师"
    reviewer.persona = "细致评审员"
    producer.memory.add("episode", "private-memory-never-shared")
    producer.post_to_blackboard("blackboard-is-not-an-assignment")
    (producer.code_workspace.root / "private.txt").write_text("private-workspace-never-shared", encoding="utf-8")
    assert fleet.pause(reviewer.fleet_id)
    projects = fleet.runs.projects
    unrelated = projects.create("不共享的项目")
    private = projects.add_work(unrelated["id"], specification(producer, title="unrelated-export-marker"))
    project = projects.create("生产和评审")
    first = projects.add_work(project["id"], specification(producer, reviewer=reviewer, title="visible-evidence-marker"))
    dependent = projects.add_work(project["id"], specification(producer, depends_on=[first["id"]], title="后续"))
    parallel = projects.add_work(project["id"], specification(producer, title="无需前置的工作"))
    fleet.runs.start()
    try:
        settle_work(fleet, private, "verified")
        parked = settle_work(fleet, first, "submitted")
        assert parked["blocked_reason"] == "fleet_paused"
        settle_work(fleet, parallel, "verified")
        assert work_view(fleet, dependent)["blocked_reason"] == f"dependency_submitted:{first['id']}"
        assert not any(kind == "review" or data["title"] == "后续" for kind, _, data in model.log)
    finally:
        fleet.runs.stop()

    restarted = AgentManager(tmp_path, llm=model)
    assert restarted.info(reviewer.fleet_id)["paused"]
    restarted.runs.start()
    try:
        assert restarted.pause(reviewer.fleet_id, False)
        accepted = settle_work(restarted, first, "verified")
        second = settle_work(restarted, dependent, "verified")
        assert accepted["latest_version"] == 1
        assert second["inputs"] == [{"work_id": first["id"], "version": 1}]
        version = restarted.runs.projects.artifact(project["id"], first["id"], 1)
        assert version["producer"] == producer.fleet_id
        assert version["review"]["reviewer"] == reviewer.fleet_id
        assert version["review"]["accepted"] == 1
        tasks = [(kind, system, data) for kind, system, data in model.log if data["title"] == "visible-evidence-marker"]
        assert [kind for kind, _, _ in tasks] == ["work", "review"]
        assert "细致评审员" in tasks[1][1]
        assert tasks[1][2]["candidate"]["files"] == version["files"]
        subsequent = next(data for _, _, data in model.log if data["title"] == "后续")
        assert subsequent["inputs"][0]["files"] == version["files"]
        shared = json.dumps(subsequent, ensure_ascii=False) + json.dumps(tasks[1][2], ensure_ascii=False)
        for secret in ("unrelated-export-marker", "blackboard-is-not-an-assignment",
                       "private-workspace-never-shared", "private-memory-never-shared"):
            assert secret not in shared
        receipts = restarted.runs.budget.for_run(accepted["review_run_id"])
        assert receipts[0]["pool"] == "owner_tasks" and receipts[0]["calls"] == 1
    finally:
        restarted.runs.stop()


def test_hard_failure_veto_and_retry_use_immutable_versions(tmp_path):
    candidates = iter(["{}", '{"proof":"reviewer veto"}', '{"proof":"validated"}'])
    decisions = iter([False, True])
    model = DeliveryModel(lambda *_: {"artifacts": {"proof.json": next(candidates)}},
                          lambda *_: {"accept": next(decisions), "note": "reviewed"})
    fleet, producer, reviewer = members(tmp_path, model)
    projects = fleet.runs.projects
    project = projects.create("重试版")
    item = projects.add_work(project["id"], specification(producer, reviewer=reviewer))
    fleet.runs.start()
    try:
        rejected = settle_work(fleet, item, "rework")
        assert rejected["reason"] == "deterministic_acceptance_failed"
        assert [kind for kind, _, _ in model.log] == ["work"]
        version = projects.artifact(project["id"], item["id"], 1)
        assert version["review"] is None
        assert not all(check["passed"] for check in version["checks"])
        second = projects.change(project["id"], item["id"], rejected["revision"], "retry")
        assert second["revision"] == 2
        with pytest.raises(CoordinationError, match="stale_work_revision"):
            projects.change(project["id"], item["id"], rejected["revision"], "retry")
        vetoed = settle_work(fleet, second, "rework")
        assert vetoed["reason"] == "review_rejected"
        assert projects.artifact(project["id"], item["id"], 2)["review"]["accepted"] == 0
        third = projects.change(project["id"], item["id"], vetoed["revision"], "retry")
        assert settle_work(fleet, third, "verified")["latest_version"] == 3
        assert projects.artifact(project["id"], item["id"], 1) == version
        final = projects.artifact(project["id"], item["id"], 3)
        assert final["files"]["proof.json"]["sha256"] == hashlib.sha256(b'{"proof":"validated"}').hexdigest()
        assert final["review"]["accepted"] == 1
    finally:
        fleet.runs.stop()


def test_dropped_prerequisite_cannot_be_skipped_or_claimed(tmp_path):
    model = DeliveryModel()
    fleet, producer, reviewer = members(tmp_path, model)
    assert fleet.pause(producer.fleet_id)
    project = fleet.runs.projects.create("必需的前置")
    first = fleet.runs.projects.add_work(project["id"], specification(producer))
    second = fleet.runs.projects.add_work(project["id"], specification(reviewer, depends_on=[first["id"]]))
    dropped = fleet.runs.projects.change(project["id"], first["id"], first["revision"], "drop")
    assert dropped["state"] == "dropped"
    fleet.runs.start()
    try:
        assert fleet.pause(producer.fleet_id, False)
        assert work_view(fleet, second)["blocked_reason"] == f"dependency_dropped:{first['id']}"
        independent = fleet.runs.projects.add_work(project["id"], specification(reviewer, title="独立路径"))
        settle_work(fleet, independent, "verified")
        waited(fleet, {"id": first["run_id"]}, "cancelled")
        assert fleet.runs.get(second["run_id"])["generation"] == 0
        assert len(model.log) == 1
    finally:
        fleet.runs.stop()


@pytest.mark.parametrize("output,reason", [
    ({"artifacts": {"../escape": "bad"}}, "output_paths_mismatch"),
    ({"artifacts": {"proof.json": "x" * 16385}}, "invalid_artifact_size_or_type"),
    ({"artifacts": {"proof.json": "{}"}, "verified": True}, "invalid_producer_reply"),
])
def test_producer_cannot_inject_outputs_or_claim_acceptance(tmp_path, output, reason):
    fleet, producer, reviewer = members(tmp_path, DeliveryModel(lambda *_: output))
    project = fleet.runs.projects.create("只接受明确输出")
    item = fleet.runs.projects.add_work(project["id"], specification(producer, reviewer=reviewer))
    fleet.runs.start()
    try:
        failed = settle_work(fleet, item, "rework")
        assert failed["reason"] == reason
        assert failed["versions"] == [] and failed["review_run_id"] is None
    finally:
        fleet.runs.stop()


def test_cancel_linearizes_before_artifact_publication(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def generate(system, data):
        entered.set()
        assert release.wait(4)
        return DeliveryModel._produce(system, data)

    fleet, producer, reviewer = members(tmp_path, DeliveryModel(generate))
    project = fleet.runs.projects.create("不发布已取消产物")
    item = fleet.runs.projects.add_work(project["id"], specification(producer, reviewer=reviewer))
    fleet.runs.start()
    try:
        assert entered.wait(3)
        fleet.runs.cancel(item["run_id"])
        release.set()
        after = settle_work(fleet, item, "rework")
        assert after["reason"] == "run_cancelled"
        assert after["versions"] == [] and after["review_run_id"] is None
    finally:
        release.set()
        fleet.runs.stop()


def test_work_assignment_is_idempotent_and_roles_bind_model_input(tmp_path):
    fleet, producer, outsider = members(tmp_path, DeliveryModel())
    projects = fleet.runs.projects
    project = projects.create("单次指派", key="project")
    assert projects.create("单次指派", key="project") == project
    with ThreadPoolExecutor(max_workers=6) as executor:
        work = list(executor.map(lambda _: projects.add_work(project["id"], specification(producer), key="work"), range(12)))
    assert len({item["run_id"] for item in work}) == 1
    assert len({item["id"] for item in work}) == 1
    with pytest.raises(CoordinationError, match="idempotency_conflict"):
        projects.add_work(project["id"], specification(producer, title="changed"), key="work")
    run, _, lock = fleet.runs._pick()
    try:
        with pytest.raises(CoordinationError, match="project_scope_denied"):
            projects._model_input(run, outsider.fleet_id)
        forged = dict(run, agent_id=outsider.fleet_id)
        with pytest.raises(CoordinationError, match="project_scope_denied"):
            projects._model_input(forged, outsider.fleet_id)
        assert projects._model_input(run, producer.fleet_id)["title"] == "基础证明"
    finally:
        lock.release()


def test_web_project_contracts_and_artifact_scope(tmp_path):
    fleet, producer, reviewer = members(tmp_path, DeliveryModel())
    with console(fleet) as server:
        assert request(server, "/api/projects", token="invalid")[0] == 401
        status, first = request(server, "/api/projects", {"name": "隔离甲"}, key="a")
        assert status == 201
        _, second = request(server, "/api/projects", {"name": "隔离乙"}, key="b")
        first_url = f"/api/projects/{first['id']}"
        second_url = f"/api/projects/{second['id']}"
        status, created = request(server, first_url + "/work", specification(producer, reviewer=reviewer), key="first-job")
        assert status == 202
        verified = settle_work(fleet, created, "verified")
        assert verified["review_run_id"]
        assert request(server, first_url + "/work", specification(producer, reviewer=producer))[0] == 400
        assert request(server, first_url + "/work", specification(producer, outputs=[{"path": "../wrong"}]))[0] == 400
        assert request(server, second_url + "/work", specification(producer, depends_on=[created["id"]]))[0] == 404
        assert request(server, second_url)[1]["work"] == []
        artifact = f"/work/{created['id']}/artifacts/1"
        assert request(server, second_url + artifact)[0] == 404
        assert request(server, first_url + artifact)[1]["review"]["reviewer"] == reviewer.fleet_id
        assert request(server, f"{first_url}/work/{created['id']}/drop", {"revision": verified["revision"]})[0] == 409


def test_m66_database_upgrades_in_place(tmp_path):
    database = tmp_path / ".coordination" / "state.sqlite3"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.executescript(_SCHEMA + "\nPRAGMA user_version = 1; UPDATE fleet_limits SET tokens = 12345;")
    fleet, _, _ = members(tmp_path)
    assert fleet.runs.budget.view()["caps"]["tokens"] == 12345
    assert fleet.runs.projects.create("迁移成功")["name"] == "迁移成功"
    with fleet.runs.database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_same_budget_solo_and_team_scenario(tmp_path):
    results = []
    for team in (False, True):
        model = DeliveryModel()
        fleet, producer, reviewer = members(tmp_path / ("team" if team else "solo"), model)
        producer.persona = "证据工程师"
        fleet.runs.budget.configure(5000, 20)
        fleet.runs.start()
        try:
            entered = time.monotonic()
            project = fleet.runs.projects.create("等价交付")
            first = fleet.runs.projects.add_work(project["id"], specification(producer, reviewer=reviewer if team else None))
            second = fleet.runs.projects.add_work(project["id"], specification(producer, title="汇总证明",
                                                                               reviewer=reviewer if team else None,
                                                                               depends_on=[first["id"]]))
            settle_work(fleet, second, "verified")
            proof = fleet.runs.projects.artifact(project["id"], second["id"], 1)
            assert json.loads(proof["files"]["proof.json"]["text"])["strategy"] == "验证优先"
            count = sum(work["state"] == "verified" for work in fleet.runs.projects.view(project["id"])["work"])
            results.append({"agents": 2 if team else 1, "accepted": count, "assigned": 2, "owner_actions": 3,
                            "elapsed_ms": round((time.monotonic() - entered) * 1000, 1), **fleet.runs.budget.view()["used"]})
        finally:
            fleet.runs.stop()
    assert [record["accepted"] for record in results] == [2, 2]
    assert [record["calls"] for record in results] == [2, 4]
    assert [record["tokens"] for record in results] == [200, 400]
    print(json.dumps(results, ensure_ascii=False))


def test_persona_changes_delivery_strategy_not_only_tone(tmp_path):
    fleet, first, second = members(tmp_path, DeliveryModel())
    first.persona = "证据工程师"
    second.persona = "最小交付者"
    project = fleet.runs.projects.create("同需求不同取舍")
    artifacts = [fleet.runs.projects.add_work(project["id"], specification(agent)) for agent in (first, second)]
    fleet.runs.start()
    try:
        strategies = []
        for item in artifacts:
            settle_work(fleet, item, "verified")
            artifact = fleet.runs.projects.artifact(project["id"], item["id"], 1)
            strategies.append(json.loads(artifact["files"]["proof.json"]["text"])["strategy"])
        assert strategies == ["验证优先", "最小化交付"]
    finally:
        fleet.runs.stop()
