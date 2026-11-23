import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from proto.exec.agentic import AgenticLoop, LOOP_SYSTEM
from proto.coordination.coordination import (
    _DIALOGUE_SCHEMA, _MODEL_PRESSURE_SCHEMA, _PROJECT_SCHEMA, _SCHEMA, CoordinationError,
)
from proto.fleet.manager import AgentManager
from tests.test_m66_fleet_runs import console, request


def role(directory, company_id, title):
    return next(position for position in directory.view(company_id)["positions"] if position["title"] == title)


def change_position(directory, company_id, position, **changes):
    revision = directory.view(company_id)["revision"]
    spec = {field: position[field] for field in ("title", "manager_id", "responsibility", "agent_id")}
    return directory.save_position(company_id, position["id"], {**spec, "revision": revision, **changes})


def appoint(fleet, company_id, title, name):
    agent = fleet.get(fleet.create(name)["id"])
    directory = fleet.runs.companies
    position = role(directory, company_id, title)
    change_position(directory, company_id, position, agent_id=agent.fleet_id)
    return agent, position["id"]


@pytest.mark.parametrize(("template", "size", "root_width", "depth"), [
    ("flat", 4, 3, 1), ("functional", 8, 3, 2), ("divisional", 7, 2, 2), ("custom", 1, 0, 0),
])
def test_templates_create_distinct_position_trees(tmp_path, template, size, root_width, depth):
    fleet = AgentManager(tmp_path)
    directory = fleet.runs.companies
    company = directory.create("示例公司", template, key="first-create")
    assert directory.create("示例公司", template, key="first-create") == company
    assert len(directory.list()) == 1 and fleet.list() == []
    preview = next(record for record in directory.templates() if record["id"] == template)
    positions = directory.view(company["id"])["positions"]
    by_id = {position["id"]: position for position in positions}
    root = [position for position in positions if position["manager_id"] is None]
    assert len(root) == 1
    assert len(positions) == len(preview["positions"]) == size
    assert sum(position["manager_id"] == root[0]["id"] for position in positions) == root_width
    levels = []
    for position in positions:
        current, level = position, 0
        assert position["agent_id"] is None and position["routes"] == []
        while current["manager_id"]:
            current, level = by_id[current["manager_id"]], level + 1
        levels.append(level)
    assert max(levels) == depth
    assert sorted(position["title"] for position in positions) == sorted(row["title"] for row in preview["positions"])


def test_api_on_empty_fleet_and_appointment_without_changing_autonomy(tmp_path):
    fleet = AgentManager(tmp_path)
    with console(fleet) as server:
        company_url = "/api/companies"
        assert request(server, company_url, token="bad-token")[0] == 401
        assert request(server, company_url, {"name": "样例", "extra": True})[0] == 400
        assert request(server, company_url, {"name": "样例", "template": []})[0] == 400
        status, catalog = request(server, company_url)
        assert status == 200 and catalog["companies"] == []
        assert {template["id"] for template in catalog["templates"]} == {"flat", "functional", "divisional", "custom"}
        status, company = request(server, company_url, {"name": "云间", "template": "custom"}, key="my-company")
        assert status == 201
        assert request(server, company_url, {"name": "另一个名称"}, key="my-company") == (409, {"error": "idempotency_conflict"})
        url = f"{company_url}/{company['id']}"
        root = request(server, url)[1]["positions"][0]
        assert root["agent_id"] is None
        assert request(server, "/api/agents/preview", {"name": "草稿"})[0] == 200
        assert fleet.list() == []
        existing = request(server, "/api/agents", {"name": "默认成员"})[1]["created"]["id"]
        drafted = request(server, "/api/agents", {"name": "草稿", "select": False})[1]["created"]["id"]
        agent = fleet.get(drafted)
        baseline = agent.persona, agent.autonomy_view()
        with fleet.ops_lock(drafted), agent.dialogue_lock:
            status, saved = request(server, f"{url}/positions/{root['id']}", {
                "revision": 1, "title": "首席工程师", "manager_id": None, "agent_id": drafted,
                "responsibility": "管理产品交付，不能扩权"})
            assert (status, saved["revision"]) == (200, 2)
        assert fleet.current_id() == existing and baseline == (agent.persona, agent.autonomy_view())
        assert request(server, url)[1]["positions"][0]["agent_id"] == drafted
        fleet.set_current(drafted)
        assert request(server, "/api/blackboard") == (200, {"notes": [], "company_bound": True})
        assert request(server, "/api/blackboard", {"text": "不可发送到公共黑板"}) == (409, {"error": "company_channel_required"})
        assert request(server, "/api/projects") == (200, {"projects": []})
        assert request(server, "/api/runs")[1]["runs"] == []


def test_position_tree_rejects_cycles_cross_company_links_and_stale_writes(tmp_path):
    directory = AgentManager(tmp_path).runs.companies
    company_id = directory.create("组织一", "functional")["id"]
    elsewhere = directory.create("组织二", "custom")["id"]
    root = role(directory, company_id, "公司负责人")
    manager = role(directory, company_id, "研发负责人")
    engineer = role(directory, company_id, "软件工程师")
    other = role(directory, elsewhere, "负责人")
    initial = directory.view(company_id)
    with pytest.raises(CoordinationError, match="company_root_locked"):
        change_position(directory, company_id, root, manager_id=engineer["id"])
    with pytest.raises(CoordinationError, match="organization_cycle"):
        change_position(directory, company_id, manager, manager_id=engineer["id"])
    with pytest.raises(CoordinationError, match="organization_cycle"):
        change_position(directory, company_id, engineer, manager_id=engineer["id"])
    with pytest.raises(CoordinationError, match="invalid_manager_id"):
        change_position(directory, company_id, engineer, manager_id=other["id"])
    with pytest.raises(CoordinationError, match="no_such_agent"):
        change_position(directory, company_id, engineer, agent_id="nonexistent")
    with pytest.raises(CoordinationError, match="invalid_company_revision"):
        change_position(directory, company_id, engineer, revision=True)
    with pytest.raises(CoordinationError, match="position_not_removable"):
        directory.remove_position(company_id, manager["id"], 1)
    assert directory.view(company_id) == initial
    new_position = directory.save_position(company_id, None, {"title": "自定义专员", "manager_id": engineer["id"],
                                                              "agent_id": None, "revision": 1})
    with pytest.raises(CoordinationError, match="stale_company_revision"):
        change_position(directory, company_id, engineer, revision=1)
    assert role(directory, company_id, "自定义专员")["manager_id"] == engineer["id"]
    directory.remove_position(company_id, new_position["id"], 2)
    assert [row["id"] for row in directory.view(company_id)["positions"]] == [row["id"] for row in initial["positions"]]


def test_only_one_company_can_appoint_an_agent_even_with_concurrent_requests(tmp_path):
    fleet = AgentManager(tmp_path)
    directory = fleet.runs.companies
    agent_id = fleet.create("唯一 Agent")["id"]
    company_ids = [directory.create(name, "custom")["id"] for name in ("甲公司", "乙公司")]

    def appoint_root(company_id):
        try:
            change_position(directory, company_id, role(directory, company_id, "负责人"), agent_id=agent_id)
            return "appointed"
        except CoordinationError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(appoint_root, company_ids)) == ["agent_already_appointed", "appointed"]
    assert sum(role(directory, company_id, "负责人")["agent_id"] == agent_id for company_id in company_ids) == 1


def test_messages_and_fleet_visibility_follow_org_edges(tmp_path):
    fleet = AgentManager(tmp_path)
    directory = fleet.runs.companies
    company_id = directory.create("独立研发", "functional")["id"]
    founder, founder_position = appoint(fleet, company_id, "公司负责人", "Founder")
    supervisor, supervisor_position = appoint(fleet, company_id, "研发负责人", "Supervisor")
    engineer, engineer_position = appoint(fleet, company_id, "软件工程师", "Engineer")
    tester, tester_position = appoint(fleet, company_id, "测试工程师", "Tester")
    other_company = directory.create("另一公司", "custom")["id"]
    outside, outside_position = appoint(fleet, other_company, "负责人", "OtherCompany")
    unbound = fleet.get(fleet.create("Unaffiliated")["id"])
    public_id = unbound.post_to_blackboard("公开信息")
    assert "公开信息" in unbound.tools.get("blackboard_read")()
    for operation in (lambda: engineer.tools.get("blackboard_read")(),
                      lambda: engineer.tools.get("blackboard_post")("不可公开"),
                      lambda: engineer.answer_blackboard(public_id, "不可公开")):
        with pytest.raises(CoordinationError, match="company_channel_required"):
            operation()
    assert not unbound.read_blackboard()[0]["replies"]
    targets = json.loads(engineer.tools.get("company_context")())["targets"]
    assert {(item["id"], item["kind"]) for item in targets} == {
        (supervisor_position, "report"), (tester_position, "coordination")}
    assert {row["name"] for row in engineer._fleet_status_fn()} == {"Supervisor", "Tester"}
    assert unbound._fleet_status_fn() == []
    with pytest.raises(ValueError):
        engineer.tools.validate("company_send", {"position_id": supervisor_position, "text": "hi", "from_agent_id": founder.fleet_id})
    for destination in (founder_position, outside_position, engineer_position):
        with pytest.raises(CoordinationError, match="unreachable_position"):
            engineer.tools.get("company_send")(destination, "不能送达")
    with pytest.raises(CoordinationError, match="no_company_position"):
        unbound.tools.get("company_send")(engineer_position, "不能送达")
    assert directory.view(company_id)["messages"] == []
    assert "report" in engineer.tools.get("company_send")(supervisor_position, "构建结果")
    assert "guidance" in supervisor.tools.get("company_send")(tester_position, "核对构建")
    assert "coordination" in tester.tools.get("company_send")(engineer_position, "请附报告")
    assert "构建结果" in supervisor.tools.get("company_inbox")()
    assert "请附报告" in engineer.tools.get("company_inbox")()
    assert "构建结果" not in founder.tools.get("company_inbox")()
    assert "构建结果" not in outside.tools.get("company_inbox")()
    assert fleet.runs.list() == []


def test_reorg_applies_immediately_and_archival_keeps_private_history(tmp_path):
    fleet = AgentManager(tmp_path)
    directory = fleet.runs.companies
    company_id = directory.create("事业群", "divisional")["id"]
    alpha, alpha_position = appoint(fleet, company_id, "事业部 A 负责人", "甲")
    beta, beta_position = appoint(fleet, company_id, "事业部 B 负责人", "乙")
    engineer, engineer_position = appoint(fleet, company_id, "A 部工程专员", "丙")
    directory.send(alpha.fleet_id, engineer_position, "只给原任看的消息")
    change_position(directory, company_id, role(directory, company_id, "A 部工程专员"), manager_id=beta_position)
    with pytest.raises(CoordinationError, match="unreachable_position"):
        directory.send(alpha.fleet_id, engineer_position, "重组后不通")
    with pytest.raises(CoordinationError, match="unreachable_position"):
        directory.send(engineer.fleet_id, alpha_position, "重组后不通")
    assert directory.send(engineer.fleet_id, beta_position, "新汇报关系")["kind"] == "report"
    assert directory.inbox(engineer.fleet_id)[0]["text"] == "只给原任看的消息"
    assert fleet.archive(engineer.fleet_id)
    restarted = AgentManager(tmp_path)
    directory = restarted.runs.companies
    archived = role(directory, company_id, "A 部工程专员")
    assert archived["agent_id"] is None and archived["archived_agent_id"] == engineer.fleet_id
    assert directory.role(engineer.fleet_id) is None
    with pytest.raises(CoordinationError, match="position_vacant"):
        directory.send(beta.fleet_id, engineer_position, "职位暂空")
    replacement, replacement_position = appoint(restarted, company_id, "A 部工程专员", "继任者")
    assert replacement_position == engineer_position
    assert directory.inbox(replacement.fleet_id) == []
    assert len(directory.view(company_id)["messages"]) == 2
    directory.send(beta.fleet_id, engineer_position, "新来信")
    assert [message["text"] for message in directory.inbox(replacement.fleet_id)] == ["新来信"]


def test_owner_message_route_uses_revision_and_labels_source(tmp_path):
    fleet = AgentManager(tmp_path)
    directory = fleet.runs.companies
    company_id = directory.create("样例公司", "flat")["id"]
    boss, boss_position = appoint(fleet, company_id, "公司负责人", "负责人")
    employee, employee_position = appoint(fleet, company_id, "产品专员", "产品专家")
    with console(fleet) as server:
        path = f"/api/companies/{company_id}/messages"
        body = {"from_position_id": employee_position, "to_position_id": boss_position, "text": "里程碑 1", "revision": 2}
        assert request(server, path, body) == (409, {"error": "stale_company_revision"})
        body["revision"] = 3
        assert request(server, path, dict(body, to_position_id=employee_position)) == (409, {"error": "unreachable_position"})
        assert request(server, path, dict(body, to_position_id=uuid4().hex)) == (409, {"error": "unreachable_position"})
        status, receipt = request(server, path, body)
        assert status == 201 and receipt["kind"] == "report"
        message = request(server, f"/api/companies/{company_id}")[1]["messages"][0]
        assert (message["id"], message["source"], message["from_agent_id"], message["to_agent_id"]) == (
            receipt["id"], "owner", employee.fleet_id, boss.fleet_id)


def test_actor_sees_current_role_and_sends_through_the_real_tool_router(tmp_path, monkeypatch):
    fleet = AgentManager(tmp_path)
    directory = fleet.runs.companies
    company_id = directory.create("有界示例", "flat")["id"]
    leader, leader_position = appoint(fleet, company_id, "公司负责人", "负责人")
    employee, _ = appoint(fleet, company_id, "工程专员", "工程师")
    prompts = []

    def reply(client, system, prompt, **kwargs):
        assert system == LOOP_SYSTEM
        prompts.append(prompt)
        if len(prompts) == 1:
            return json.dumps({"tool": "company_send", "args": {"position_id": leader_position, "text": "样例结果"}})
        return json.dumps({"done": True, "summary": "已汇报样例"})

    monkeypatch.setattr(employee, "llm", object())
    monkeypatch.setattr(employee, "_complete", reply)
    result = AgenticLoop(employee, max_steps=2).run("发送示例进度")
    assert result["status"] == "completed"
    assert all("组织任职事实" in prompt and "有界示例" in prompt for prompt in prompts)
    assert directory.inbox(leader.fleet_id)[0]["text"] == "样例结果"
    assert fleet.runs.list() == []


def test_schema_four_migrates_without_touching_existing_records(tmp_path):
    old_path = tmp_path / ".coordination" / "state.sqlite3"
    old_path.parent.mkdir()
    with sqlite3.connect(old_path) as connection:
        connection.executescript(_SCHEMA + _PROJECT_SCHEMA + _DIALOGUE_SCHEMA + _MODEL_PRESSURE_SCHEMA + """
            INSERT INTO model_pressure VALUES ('example-route', 100, 3);
            INSERT INTO projects VALUES ('example', 'existing project', '', NULL, 1);
            PRAGMA user_version = 4;
        """)
    fleet = AgentManager(tmp_path)
    assert fleet.runs.companies.list() == []
    assert fleet.runs.projects.view("example")["name"] == "existing project"
    with fleet.runs.database.connect() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert connection.execute("SELECT strikes FROM model_pressure WHERE route = 'example-route'").fetchone()[0] == 3
