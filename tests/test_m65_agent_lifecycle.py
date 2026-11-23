import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from proto.exec.agentic import AgenticLoop
from proto.exec.approval import ActionProposal
from proto.exec.router import Task
from proto.tools.webtools import WebToolkit
from proto.kernel.kernel import ActionCategory
from proto.runtime.agent import ProtoAgent
from proto.runtime.manager import AgentManager
from proto.runtime.personas import PERSONA_DEFAULTS
from proto.runtime.timeout_policy import TimeoutDecision


SOFTWARE, MENTOR = list(PERSONA_DEFAULTS)[:2]
SOFTWARE_GOAL = PERSONA_DEFAULTS[SOFTWARE]["seed_goal"]


class ScriptModel:
    def __init__(self, recommendation="{}"):
        self.calls = []
        self.decisions = []
        self.recommendation = recommendation

    def complete(self, system, user):
        self.calls.append(system)
        if "任务执行循环" in system:
            decision = self.decisions.pop(0)
            return json.dumps(decision() if callable(decision) else decision)
        if "推荐 4-6 个内在参数" in system:
            return self.recommendation
        if "独立验收" in system:
            return '{"verdict":"pass","missing":[]}'
        return "可以。"


def no_network(*args):
    pytest.fail("unexpected network request")


@pytest.fixture()
def manager(tmp_path):
    return AgentManager(tmp_path / "fleet", llm=ScriptModel(), webtools=WebToolkit(fetch=no_network))


def member(manager, **fields):
    return manager.get(manager.create("成员", **fields)["id"])


def forbid_schedule(agent, monkeypatch):
    monkeypatch.setattr(agent.scheduler, "tick", lambda: pytest.fail("unexpected schedule"))


def register_effect(agent, effects):
    def record_report(report: str):
        effects.append(report)
        return report

    agent.tools.register("record_report", record_report, category=ActionCategory.VISIBLE_WRITE,
                         owner_only=True)
    agent.llm.decisions = [{"tool": "record_report", "args": {"report": "approved-report"}},
                           {"done": True, "summary": "报告已保存"}]


def preview_payload():
    return {"name": "构建组", "persona": SOFTWARE, "priority": "补齐本地交付检查", "mode": "auto"}


def test_static_preview_and_first_boot_never_call_models(tmp_path, manager):
    target = tmp_path / "not-created"
    preview = AgentManager.preview_at(target, **preview_payload())
    assert not target.exists()
    assert preview["scope"]["budget_caps"]["self_initiated"]["tokens"] > 0
    assert preview["profile"]["params"]["personality.rigor"] == 0.85
    registry = manager.root / "registry.json"
    assert manager.preview(**preview_payload())["autonomy"]["enabled"] is False
    assert not registry.exists()
    draft = manager.get(manager.create(**preview_payload())["id"])
    assert draft.state.get("personality.rigor") == 0.85
    assert [node.text for node in draft.goal_tree.all_active()] == [SOFTWARE_GOAL]
    assert draft._priority_seed_dirty and draft.autonomy_blocker() == "not_enabled"
    assert manager._llm.calls == []


def test_persona_converges_at_creation_edit_and_restart(manager):
    created = member(manager, persona=SOFTWARE)
    edited = member(manager)
    profile = edited.set_persona(SOFTWARE)
    assert profile["defaults_status"] == "applied"
    assert created.state.to_dict() == edited.state.to_dict()
    original_goal = created.goal_tree.active_long_term()[0]
    created.goal_tree.drop(original_goal.id)
    created.set_persona(MENTOR)
    assert created.state.get("personality.rigor") == 0.6
    assert created.state.get("instincts.risk_tolerance") == 0.4
    created.set_persona(SOFTWARE)
    reopened = AgentManager(manager.root).get(created.fleet_id)
    assert reopened.profile == created.profile
    assert reopened.fingerprint == created.fingerprint
    matches = [node for node in reopened.goal_tree.snapshot()["nodes"] if node["text"] == SOFTWARE_GOAL]
    assert [(node["id"], node["status"]) for node in matches] == [(original_goal.id, "dropped")]
    assert manager._llm.calls == []


def test_switching_templates_keeps_effective_learning_and_owner_values(manager):
    agent = member(manager, persona=SOFTWARE)
    agent.mgr.apply_modulation("instincts.exploration", -0.04, "feedback")
    agent.owner_set("personality.rigor", 0.7)
    agent.lock_param("personality.directness", True)
    agent.mgr.freeze_dormant("instincts.social_initiative", "test")
    agent.set_persona(MENTOR)
    reopened = AgentManager(manager.root).get(agent.fleet_id)
    for path, value, origin in (
            ("instincts.exploration", 0.66, "learned"), ("personality.rigor", 0.7, "owner"),
            ("personality.directness", 0.7, "template"),
            ("instincts.social_initiative", agent.state.get("instincts.social_initiative"), "factory")):
        assert reopened.state.get(path) == value
        assert reopened.state.spec(path)["origin"] == origin
    assert reopened.state.spec("personality.directness")["owner_locked"]


def test_legacy_migration_and_repeated_adoption_share_fleet_tools(tmp_path, manager):
    existing = ProtoAgent(tmp_path / "legacy", webtools=WebToolkit(fetch=no_network))
    existing.owner_set("personality.rigor", 0.7)
    existing.mgr.apply_modulation("instincts.exploration", 0.01, "feedback")
    store = existing.spine.store
    raw = existing.state.to_dict()
    for group in ("instincts", "personality"):
        for param in raw[group].values():
            param.pop("origin", None)
    store.put("state", "main", raw)
    store.put("config", "persona", {"text": SOFTWARE})
    store.delete("config", "autonomy")
    legacy = ProtoAgent(existing.data_dir, webtools=WebToolkit(fetch=no_network))
    fresh = member(manager)
    first = manager.adopt(legacy, "旧成员")
    assert manager.adopt(legacy, "不改名") == first
    assert legacy.fingerprint == existing.fingerprint
    assert legacy.autonomy_blocker() == ""
    assert legacy.state.get("personality.rigor") == 0.7
    assert legacy.state.get("instincts.exploration") == 0.61
    assert legacy.state.get("personality.directness") == 0.7
    assert legacy.goal_tree.active_long_term()[0].text == SOFTWARE_GOAL
    fresh.tools.get("blackboard_post")("新的交接信息")
    assert "新的交接信息" in legacy.tools.get("blackboard_read")()
    legacy.tools.get("blackboard_post")("来自旧成员")
    assert "来自旧成员" in fresh.tools.get("blackboard_read")()
    assert "成员" in legacy.tools.get("fleet_status")()
    manager.pause(first["id"])
    rebooted = AgentManager(manager.root)
    restored = ProtoAgent(legacy.data_dir, webtools=WebToolkit(fetch=no_network))
    assert rebooted.adopt(restored)["id"] == first["id"]
    assert restored.autonomy_blocker() == "fleet_paused"
    assert len(restored.goal_tree.active_long_term()) == 1


def test_rename_preserves_adopted_identity_and_pending_work_on_restart(tmp_path, manager, monkeypatch):
    existing = ProtoAgent(tmp_path / "legacy workspace", persona=SOFTWARE)
    selected = manager.create("同名")["id"]
    registered = manager.adopt(existing, "旧名称")
    agent_id = registered["id"]
    assert manager.pause(agent_id)
    run = manager.runs.enqueue(agent_id, "task", {"text": "仍由原成员处理"})
    registry = manager.root / "registry.json"
    expected = json.loads(registry.read_text())
    next(row for row in expected["agents"] if row["id"] == agent_id)["name"] = "同名"

    result = {"id": agent_id, "name": "同名"}
    assert manager.rename(agent_id, "  同名  ") == result
    assert json.loads(registry.read_text()) == expected
    assert manager.get(agent_id) is existing
    monkeypatch.setattr(manager, "_save_registry", lambda _: pytest.fail("unchanged name rewrote registry"))
    assert manager.rename(agent_id, "同名") == result

    reopened = AgentManager(manager.root)
    assert reopened.current_id() == selected
    assert reopened.info(agent_id)["name"] == reopened.info(selected)["name"] == "同名"
    assert reopened.runs.get(run["id"]) == run
    loaded = reopened.get(agent_id)
    assert loaded.fleet_id == agent_id
    assert loaded.data_dir == existing.data_dir
    assert loaded.fingerprint == existing.fingerprint
    assert loaded.profile == existing.profile
    assert loaded.autonomy_blocker() == "fleet_paused"
    assert manager._llm.calls == []


@pytest.mark.parametrize("recommendation", [
    "no-json", '{"params":{"desires.curiosity_gap":0.9},"seed_goal":"坏目标"}',
    '{"params":{"personality.rigor":true},"seed_goal":"坏目标"}',
    '{"params":{"personality.rigor":NaN},"seed_goal":"坏目标"}',
    '{"params":{"personality.rigor":0.9},"seed_goal":"坏目标","mode":"auto"}',
])
def test_free_text_keeps_previous_defaults_on_invalid_recommendation(manager, recommendation):
    agent = member(manager, persona=SOFTWARE)
    agent.llm_fast.recommendation = recommendation
    profile = agent.set_persona("自由文本")
    assert agent.persona == "自由文本" and profile["defaults_status"] == "pending"
    assert agent.state.get("personality.rigor") == 0.85
    assert [node.text for node in agent.goal_tree.all_active()] == [SOFTWARE_GOAL]
    assert agent.autonomy_blocker() == "not_enabled"


def test_free_text_starts_pending_and_applies_only_on_explicit_save(manager):
    agent = member(manager, persona="务实的资料整理员")
    assert agent.profile.defaults_status == "pending" and not manager._llm.calls
    agent.llm_fast.recommendation = '{"params":{"personality.rigor":0.8},"seed_goal":"整理可查资料"}'
    assert agent.set_persona(agent.persona)["defaults_status"] == "applied"
    assert agent.state.get("personality.rigor") == 0.8
    assert not agent.autonomy_view()["enabled"]


@pytest.mark.parametrize("blocked", ["not_enabled", "manual_only", "suggestions_only", "fleet_paused", "muted"])
def test_all_autonomous_ingress_stops_but_owner_steps_still_run(manager, monkeypatch, blocked):
    agent = member(manager, priority="仅记录文本")
    agent.configure_autonomy(enabled=blocked != "not_enabled",
                             mode={"manual_only": "manual", "suggestions_only": "suggest"}.get(blocked, "auto"))
    if blocked == "fleet_paused":
        manager.pause(agent.fleet_id)
        agent.kernel.set_mute(False)
    if blocked == "muted":
        agent.kernel.set_mute(True)
    node = agent.goal_tree.add("主人已有目标", "task", source="owner")
    before = agent.state.to_dict()
    forbid_schedule(agent, monkeypatch)
    pending = agent.queue.submit(ActionProposal(ActionCategory.VISIBLE_WRITE, "待批", {}, task_id="pending"))
    monkeypatch.setattr(agent, "owner_timeout_s", -1)
    agent._maybe_timeout_decide()
    manager.tick_all()
    assert agent.force_act()["reason_code"] == blocked
    assert agent.work_on_goal(node)["reason"] == blocked
    assert agent.state.to_dict() == before
    assert agent._priority_seed_dirty and manager._llm.calls == []
    assert agent.queue.pending()[0].proposal_id == pending.proposal_id
    assert agent.run_task_with_steps("主人读时间", [{"tool": "clock", "args": {}}])["status"] == "completed"


def test_suggestion_is_deduplicated_and_pending_priority_survives_restart(manager, monkeypatch):
    agent = member(manager, priority="优先补工作说明")
    agent.configure_autonomy(enabled=True, mode="suggest")
    suggestion = agent.maybe_proactive()
    assert "优先补工作说明" in suggestion
    assert agent.maybe_proactive() is None
    restored = AgentManager(manager.root, webtools=WebToolkit(fetch=no_network)).get(agent.fleet_id)
    assert restored.autonomy_view()["suggestion"] == suggestion
    assert restored._priority_seed_dirty and restored.maybe_proactive() is None
    assert not restored.goal_tree.all_active()
    restored.configure_autonomy(mode="auto")
    for name in ("_morning_brief", "_review_predictions", "maybe_reflect"):
        monkeypatch.setattr(restored, name, lambda: None)
    monkeypatch.setattr(restored, "_may_start_chain", lambda: False)
    restored.maybe_proactive()
    assert restored.goal_tree.active_long_term()[0].text == "优先补工作说明"
    assert not restored._priority_seed_dirty


@pytest.mark.parametrize("raw", ["null", "invalid json", '{"schema_version":2,"enabled":true,"mode":"auto","revision":1}'])
def test_corrupt_policy_never_falls_back_to_legacy_auto(manager, raw):
    agent = member(manager)
    agent.configure_autonomy(enabled=True, mode="auto")
    (agent.data_dir / "data/config/autonomy.json").write_text(raw, encoding="utf-8")
    restored = AgentManager(manager.root).get(agent.fleet_id)
    assert restored.autonomy_blocker() == "not_enabled"
    assert restored.autonomy_view()["revision"] == 0
    assert restored.configure_autonomy(enabled=True, mode="manual", expected_revision=0)["revision"] == 1


def test_managed_agent_with_deleted_policy_fails_closed(manager):
    agent = member(manager)
    agent.configure_autonomy(enabled=True, mode="auto")
    agent.spine.store.delete("config", "autonomy")
    restored = AgentManager(manager.root).get(agent.fleet_id)
    assert restored.autonomy_blocker() == "not_enabled"


def test_owner_budget_is_not_auto_origin_and_downgrade_keeps_approval(manager):
    agent = member(manager)
    agent.configure_autonomy(enabled=True, mode="auto")
    goal = agent.goal_tree.add("主人已有目标", "task", source="owner")
    effects = []
    register_effect(agent, effects)
    paused = AgenticLoop(agent).run("推进已有目标", goal_id=goal.id)
    assert paused["status"] == "paused"
    session = agent.sessions.lookup(paused["task_id"])
    assert session["context"]["pool"] == "owner_tasks" and session["autonomous"] is True
    agent.configure_autonomy(mode="manual")
    assert agent.resume_task(paused["task_id"], True)["reason"] == "manual_only"
    assert agent.accept_proposal(paused["proposal_id"])["reason"] == "manual_only"
    assert agent.sessions.lookup(paused["task_id"])["status"] == "awaiting_approval"
    assert effects == [] and agent.queue.pending()
    assert agent.resume_task(paused["task_id"], False)["status"] == "denied"


def test_owner_task_can_resume_while_fleet_is_paused(manager):
    agent = member(manager)
    effects = []
    register_effect(agent, effects)
    pending = AgenticLoop(agent).run("主人明确指派")
    assert pending["status"] == "paused"
    manager.pause(agent.fleet_id)
    assert agent.resume_task(pending["task_id"], True)["status"] in ("verified", "completed")
    assert effects == ["approved-report"]


def test_in_flight_model_result_cannot_start_new_effect(manager):
    agent = member(manager)
    agent.configure_autonomy(enabled=True, mode="auto")
    goal = agent.goal_tree.add("自主推进但走主人资金池", "task", source="owner")

    def downgrade():
        agent.configure_autonomy(mode="suggest")
        return {"tool": "write_file", "args": {"path": "would-write.md", "content": "blocked"}}

    agent.llm.decisions = [downgrade]
    result = AgenticLoop(agent).run("自主推进", goal_id=goal.id)
    assert result["status"] == "cancelled" and result["reason"] == "suggestions_only"
    assert not (agent.code_workspace.root / "would-write.md").exists()


def test_router_rechecks_between_effects_and_does_not_change_pool(manager):
    agent = member(manager)
    agent.configure_autonomy(enabled=True, mode="auto")
    notes = []

    def note(text: str):
        notes.append(text)
        agent.configure_autonomy(mode="manual")
        return text

    agent.tools.register("note", note)
    before = agent.kernel.pools.remaining_all()
    task = Task("two-steps", "既有目标", [{"tool": "note", "args": {"text": text}}
                                       for text in ("first", "second")], autonomous=True)
    result = agent.router.run(task)
    after = agent.kernel.pools.remaining_all()
    assert result.status == "blocked" and notes == ["first"]
    assert after["owner_tasks"]["tokens"] < before["owner_tasks"]["tokens"]
    assert after["self_initiated"] == before["self_initiated"]


def test_timeout_rechecks_after_decision_and_before_quota(manager, monkeypatch):
    agent = member(manager)
    agent.configure_autonomy(enabled=True, mode="auto")
    monkeypatch.setattr(agent, "owner_timeout_s", -1)
    pending = agent.queue.submit(ActionProposal(ActionCategory.VISIBLE_WRITE, "低风险待批", {},
                                                task_id="timeout-test", self_initiated=True))
    quota = agent.timeout_quota.remaining()

    def decide(*args):
        manager.pause(agent.fleet_id)
        return TimeoutDecision("auto_approve", "测试")

    monkeypatch.setattr("proto.runtime.timeout_policy.decide", decide)
    agent._maybe_timeout_decide()
    assert [proposal.proposal_id for proposal in agent.queue.pending()] == [pending.proposal_id]
    assert agent.timeout_quota.remaining() == quota


def call(server, path, payload=None, *, token=True, agent_id=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Proto-Token"] = server.token
    if agent_id:
        headers["X-Proto-Agent"] = agent_id
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", data=data, headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=5)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        return response.code, json.load(response)


def test_http_preview_and_scoped_policy_updates_are_strict_and_fast(manager, monkeypatch):
    from proto.web.server import ProtoWebServer
    with ProtoWebServer(manager=manager, port=0, heartbeat_s=0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            preview_path = "/api/agents/preview"
            assert call(server, preview_path, preview_payload(), token=False)[0] == 401
            assert call(server, preview_path, preview_payload())[0] == 200
            assert manager.list() == []
            assert call(server, "/api/agents", dict(preview_payload(), enabled=True))[0] == 400
            code, created = call(server, "/api/agents", preview_payload())
            assert code == 200 and not created["created"]["autonomy"]["enabled"]
            target = manager.get(created["created"]["id"])
            other = member(manager)
            policy_path = f"/api/agents/{target.fleet_id}/autonomy"
            assert call(server, policy_path, {"enabled": True}, token=False)[0] == 401
            for invalid in ({"enabled": "false"}, {"mode": []}, {"mode": None}, {"expected_revision": 1}):
                assert call(server, policy_path, invalid)[0] == 400
            with manager.ops_lock(target.fleet_id):
                code, result = call(server, policy_path, {"enabled": True, "mode": "auto", "expected_revision": 1},
                                    agent_id=other.fleet_id)
            assert code == 200 and result["autonomy"]["blocked_reason"] == ""
            assert result["scope"]["projects_root"] == target.projects_root()
            assert call(server, policy_path, {"enabled": False, "expected_revision": 1})[0] == 409
            assert other.autonomy_blocker() == "not_enabled"
            pause = f"/api/agents/{target.fleet_id}/pause"
            assert call(server, pause, {"on": "false"})[0] == 400
            assert call(server, pause, {"on": True})[0] == 200
            assert call(server, "/api/mute", {"on": False}, agent_id=target.fleet_id)[0] == 200
            forbid_schedule(target, monkeypatch)
            assert call(server, "/api/chat", {"text": "还能对话吗"}, agent_id=target.fleet_id)[1]["reply"]
            assert call(server, "/api/tick", {}, agent_id=target.fleet_id) == (200, {"proposal": None})
            assert call(server, "/api/state", agent_id=target.fleet_id)[1]["autonomy"]["blocked_reason"] == "fleet_paused"
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_http_agent_rename_is_strict_and_does_not_wait_for_agent_locks(manager, monkeypatch):
    from proto.web.server import ProtoWebServer
    target = member(manager, persona=SOFTWARE)
    other = member(manager)
    target_id = target.fleet_id
    path = f"/api/agents/{target_id}/name"
    original = manager.info(target_id)
    with ProtoWebServer(manager=manager, port=0, heartbeat_s=0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert call(server, path, {"name": "越权"}, token=False) == (401, {"error": "unauthorized"})
            for fields in ({}, {"name": "试图改 ID", "id": other.fleet_id}):
                assert call(server, path, fields) == (400, {"error": "invalid_agent_fields"})
            for name in (None, [], True, " \t ", "长" * 41, "换\n行", "空\0字", "分\u2028隔", "\ud800"):
                assert call(server, path, {"name": name}) == (400, {"error": "invalid_agent_name"})
            assert manager.info(target_id) == original

            with manager.ops_lock(target_id), target.dialogue_lock, monkeypatch.context() as patched:
                patched.setattr(manager, "get", lambda _: pytest.fail("rename loaded an Agent"))
                assert call(server, path, {"name": "  iPhone 开发组 🧠  "}, agent_id=other.fleet_id) == (
                    200, {"id": target_id, "name": "iPhone 开发组 🧠"})
            code, fleet = call(server, "/api/agents")
            assert code == 200 and fleet["current"] == target_id
            assert {agent["id"]: agent["name"] for agent in fleet["agents"]} == {
                target_id: "iPhone 开发组 🧠", other.fleet_id: "成员"}
            assert manager.archive(other.fleet_id)
            assert call(server, f"/api/agents/{other.fleet_id}/name", {"name": "新名"}) == (
                404, {"error": "no_such_agent"})
            assert manager._llm.calls == []
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_cli_preview_and_explicit_enable_use_existing_instance_lock(tmp_path, capsys):
    from proto.agents.__main__ import main
    from proto.system.instance_lock import InstanceLock
    root = tmp_path / "fleet"
    args = ["--root", str(root)]
    assert main(args + ["preview", "--name", "CLI", "--mode", "auto"]) == 0
    assert not root.exists() and json.loads(capsys.readouterr().out)["autonomy"]["enabled"] is False
    assert main(args + ["create", "--name", "CLI"]) == 0
    manager = AgentManager(root)
    command = args + ["autonomy", manager.current_id(), "--enable", "--mode", "suggest"]
    lock = InstanceLock(root)
    assert lock.acquire()
    try:
        assert main(command) == 1
        assert manager.get(manager.current_id()).autonomy_blocker() == "not_enabled"
    finally:
        lock.release()
    assert main(command) == 0
    assert AgentManager(root).get(manager.current_id()).autonomy_blocker() == "suggestions_only"


def test_console_requires_preview_then_separate_revisioned_enable():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    html = (Path(__file__).resolve().parents[1] / "proto/web/index.html").read_text()
    labels = html.split("const AUTONOMY_LABELS = ", 1)[1].split("(function renderPersonaPresets", 1)[0]
    controls = html.split("let draftAgent = null;", 1)[1].split("async function refreshGoalTree", 1)[0]
    harness = '''
const assert = require('node:assert/strict');
const fields = new Map();
function get(id) {
  if (!fields.has(id)) fields.set(id, {value: '', disabled: false, showModal() { this.open = true; }});
  return fields.get(id);
}
const document = {getElementById: get};
const location = {reload() {}};
const calls = [];
const policy = {enabled: false, revision: 1, mode: 'manual', blocked_reason: 'not_enabled'};
async function api(method, path, data) {
  calls.push([method, path, data]);
  return {preview: {name: 'Member', profile: {params: {}, defaults_status: 'pending'},
                    scope: {tools: [], budget_caps: {}}},
          name: 'Member', autonomy: policy, scope: {tools: [], budgets: {}}};
}
async function refreshFleet() {}
async function refreshState() {}
'''
    checks = '''
(async () => {
  get('agent-create-name').value = 'Member';
  await saveAgentDraft();
  assert.equal(calls.length, 0);
  await previewAgent();
  get('agent-create-persona').value = 'updated';
  await saveAgentDraft();
  assert.equal(calls.length, 1);
  await previewAgent();
  await saveAgentDraft();
  assert.equal(calls[2][1], '/api/agents');
  assert.equal(calls[2][2].enabled, undefined);
  await openAutonomy('test-id');
  get('autonomy-mode').value = 'suggest';
  await saveAutonomy(true);
  assert.deepEqual(calls[4], ['POST', '/api/agents/test-id/autonomy',
                            {enabled: true, mode: 'suggest', expected_revision: 1}]);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run([node, "-e", harness + "const AUTONOMY_LABELS = " + labels
                             + "let draftAgent = null;" + controls + checks], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_console_name_editor_binds_request_to_opened_agent_and_keeps_failed_input():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    html = (Path(__file__).resolve().parents[1] / "proto/web/index.html").read_text()
    assert "readonly" in html.split('id="agent-identity-id"', 1)[1].split(">", 1)[0]
    marker = "let agentIdentityEditor = null;"
    script = marker + html.split(marker, 1)[1].split("function toggleActivity", 1)[0]
    harness = '''
const assert = require('node:assert/strict');
const fields = new Map();
function get(id) {
  if (!fields.has(id)) fields.set(id, {showModal() { this.open = true; }});
  return fields.get(id);
}
const document = {getElementById: get};
const fleetMembers = [{id: 'selected', name: '当前成员'}, {id: 'other-agent', name: '其他成员'}];
const calls = [];
const lifecycle = [];
let respond;
function api(method, path, body, requestKey, agentId, signal) {
  if (method === 'GET') {
    assert.equal(path, '/api/identity');
    assert.equal(signal.aborted, false);
    return new Promise((resolve, reject) => lifecycle.push({agentId, resolve, reject}));
  }
  calls.push([method, path, body]);
  return new Promise((resolve, reject) => { respond = {resolve, reject}; });
}
let refreshed = 0;
async function refreshFleet() { refreshed++; }
'''
    checks = '''
(async () => {
  get('agent-select').value = 'selected';
  openAgentIdentity('other-agent');
  const id = get('agent-identity-id'), name = get('agent-identity-name');
  const lifetime = get('agent-identity-lifecycle');
  const submit = () => saveAgentName({preventDefault() {}});
  assert.equal(id.value, 'other-agent');
  assert.equal(lifecycle[0].agentId, 'other-agent');
  assert.match(lifetime.textContent, /正在读取/);
  name.value = '   ';
  const invalid = submit();
  respond.reject(new Error('invalid_agent_name'));
  await invalid;
  assert.equal(name.value, '   ');
  assert.match(get('agent-identity-message').textContent, /单行名称/);
  assert.equal(get('agent-identity-save').disabled, false);
  name.value = '新名称';
  const pending = submit();
  const duplicate = submit();
  assert.equal(calls.length, 2);
  await duplicate;
  assert.deepEqual(calls[1], ['POST', '/api/agents/other-agent/name', {name: '新名称'}]);
  get('agent-identity-panel').open = false;
  openAgentIdentity();
  assert.equal(lifecycle[1].agentId, 'selected');
  lifecycle[0].resolve({self: {lifecycle_summary: '其他成员的旧时长'}});
  respond.resolve({id: 'other-agent', name: '新名称'});
  await pending;
  assert.equal(id.value, 'selected');
  assert.equal(name.value, '当前成员');
  assert.equal(fleetMembers[1].name, '新名称');
  assert.equal(get('agent-select').value, 'selected');
  assert.equal(refreshed, 1);
  assert.match(lifetime.textContent, /正在读取/);
  lifecycle[1].reject(new Error('timeout'));
  await Promise.resolve();
  assert.match(lifetime.textContent, /仍可改名/);
  assert.equal(name.disabled, false);
  openAgentIdentity();
  lifecycle[2].resolve({self: {lifecycle_summary: '当前成员的真实时长'}});
  await Promise.resolve();
  assert.equal(lifetime.textContent, '当前成员的真实时长');
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    result = subprocess.run([node, "-e", harness + script + checks], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
