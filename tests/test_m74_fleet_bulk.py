import json
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from proto.runtime.agent import ProtoAgent
from proto.runtime.coordination import CoordinationError
from proto.runtime.manager import AgentManager
from proto.web.__main__ import _open_fleet
from tests.test_m66_fleet_runs import console, request


BULK_PATH = "/api/agents/bulk"


def archive_request(*agent_ids):
    return {"action": "archive", "confirmed": True, "agent_ids": list(agent_ids)}


def test_fleet_pause_and_resume_persist_once_without_waiting_for_agent_locks(tmp_path, monkeypatch):
    fleet = AgentManager(tmp_path)
    active_id = fleet.create("自治成员", mode="auto")["id"]
    paused_id = fleet.create("手动成员")["id"]
    active = fleet.get(active_id)
    paused = fleet.get(paused_id)
    active.configure_autonomy(enabled=True)
    paused.kernel.set_mute(True)
    fleet.set_current(paused_id)
    fleet.pause(paused_id)
    saves = []
    save_registry = fleet._save_registry

    def save(reg):
        saves.append([(row["id"], row["paused"]) for row in reg["agents"]])
        save_registry(reg)

    monkeypatch.setattr(fleet, "_save_registry", save)
    with console(fleet) as server, fleet.ops_lock(active_id), paused.dialogue_lock:
        assert request(server, BULK_PATH, {"action": "pause"}, token="wrong")[0] == 401
        assert request(server, BULK_PATH, {"action": "pause"}) == (200, {"total": 2, "changed": 1})
        assert request(server, BULK_PATH, {"action": "pause"}) == (200, {"total": 2, "changed": 0})
        assert len(saves) == 1 and all(on for _, on in saves[0])
        assert active.autonomy_blocker() == paused.autonomy_blocker() == "fleet_paused"
        code, reply = request(server, "/api/chat", {"text": "在干嘛"})
        assert code == 200 and reply["read_only"] is True
        reopened = AgentManager(tmp_path)
        assert reopened.current_id() == paused_id
        assert all(reopened.info(agent_id)["paused"] for agent_id in (active_id, paused_id))

        assert request(server, BULK_PATH, {"action": "resume"}) == (200, {"total": 2, "changed": 2})
        assert len(saves) == 2 and not any(on for _, on in saves[1])
        assert active.autonomy_blocker() == ""
        assert paused.autonomy_blocker() == "muted" and not paused.autonomy_view()["enabled"]
        assert fleet.current_id() == paused_id


def test_failed_bulk_pause_does_not_change_cached_flags(tmp_path, monkeypatch):
    fleet = AgentManager(tmp_path)
    agent = fleet.get(fleet.create("保留状态", mode="auto")["id"])
    agent.configure_autonomy(enabled=True)

    def fail_save(reg):
        raise OSError("registry temporarily read-only")

    monkeypatch.setattr(fleet, "_save_registry", fail_save)
    with pytest.raises(OSError):
        fleet.pause_all(True)
    assert agent.autonomy_blocker() == ""
    assert not fleet.info(agent.fleet_id)["paused"]


def test_archive_requires_authenticated_confirmation_of_current_membership(tmp_path):
    fleet = AgentManager(tmp_path)
    first = fleet.create("已查看")["id"]
    with console(fleet) as server:
        assert request(server, BULK_PATH, archive_request(first), token="wrong")[0] == 401
        for fields in ({"action": "archive"}, {"action": "resume", "agent_ids": []},
                       dict(archive_request(first), confirmed=1), archive_request(first, first),
                       dict(archive_request(first), agent_ids=[{"id": first}])):
            assert request(server, BULK_PATH, fields)[0] == 400
        second = fleet.create("确认后加入")["id"]
        assert request(server, BULK_PATH, archive_request(first)) == (409, {"error": "fleet_membership_changed"})
        assert {agent["id"] for agent in fleet.list()} == {first, second}
        assert not list((tmp_path / "archive").iterdir())


def test_bulk_archive_reports_busy_members_and_keeps_history_and_data(tmp_path):
    fleet = AgentManager(tmp_path)
    runner, talker, idle = [fleet.get(fleet.create(name)["id"]) for name in ("后台成员", "对话成员", "空闲成员")]
    ids = [agent.fleet_id for agent in (runner, talker, idle)]
    fleet.set_current(idle.fleet_id)
    (idle.data_dir / "evidence.txt").write_text("preserve workspace", encoding="utf-8")
    run = fleet.runs.enqueue(idle.fleet_id, "task", {"text": "保留运行记录"})
    snapshot = fleet.runs.cancel(run["id"])

    with console(fleet) as server:
        with fleet.ops_lock(runner.fleet_id), talker.dialogue_lock:
            code, result = request(server, BULK_PATH, archive_request(*reversed(ids)))
            assert code == 200 and result["total"] == 3
            assert result["archived"] == [{"id": idle.fleet_id, "name": "空闲成员", "storage": "archive"}]
            assert {row["id"]: row["error"] for row in result["skipped"]} == {
                runner.fleet_id: "agent_busy", talker.fleet_id: "chat_in_progress"}
        assert fleet.current_id() == runner.fleet_id
        entry = fleet._load_registry()["archived"][0]
        assert not idle.data_dir.exists()
        assert (tmp_path / entry["archive_dir"] / "evidence.txt").read_text() == "preserve workspace"
        assert fleet.runs.get(run["id"]) == snapshot
        assert request(server, BULK_PATH, {"action": "resume"}) == (200, {"total": 2, "changed": 0})
        assert fleet.get(idle.fleet_id) is None
        with pytest.raises(CoordinationError, match="no_such_agent"):
            with fleet.active_access(idle):
                pytest.fail("a stale Agent object was accepted")

        status, result = request(server, BULK_PATH, archive_request(runner.fleet_id, talker.fleet_id))
        assert status == 200 and len(result["archived"]) == 2 and result["skipped"] == []
        assert request(server, "/api/agents") == (200, {"current": None, "agents": []})
        assert request(server, "/api/state") == (404, {"error": "no_such_agent"})
        assert request(server, "/api/awake")[0] == 200
        assert request(server, "/api/projects")[0] == request(server, "/api/runs")[0] == 200
        assert request(server, BULK_PATH, {"action": "resume"}) == (200, {"total": 0, "changed": 0})
        status, created = request(server, "/api/agents", {"name": "新成员"})
        assert status == 200 and created["created"]["id"] not in ids


def test_archive_skips_agent_with_inflight_fast_web_control(tmp_path, monkeypatch):
    fleet = AgentManager(tmp_path)
    agent = fleet.get(fleet.create("正在写配置")["id"])
    entered, release = threading.Event(), threading.Event()
    set_priority = agent.set_priority_text

    def hold_priority(text):
        entered.set()
        assert release.wait(4)
        return set_priority(text)

    monkeypatch.setattr(agent, "set_priority_text", hold_priority)
    with console(fleet) as server, ThreadPoolExecutor() as executor:
        pending = executor.submit(request, server, "/api/priority", {"text": "先保留写入"})
        try:
            assert entered.wait(3)
            code, result = request(server, BULK_PATH, archive_request(agent.fleet_id))
            assert code == 200 and result["archived"] == []
            assert result["skipped"][0]["error"] == "agent_busy"
            assert request(server, BULK_PATH, {"action": "pause"}) == (200, {"total": 1, "changed": 1})
        finally:
            release.set()
        assert pending.result(timeout=3)[0] == 200
        assert agent.state.owner_priority == "先保留写入"


def test_archive_does_not_move_unreviewed_members_added_during_batch(tmp_path, monkeypatch):
    fleet = AgentManager(tmp_path)
    first = fleet.create("已确认")["id"]
    try_archive = fleet._try_archive

    def create_before_moving(agent_id):
        fleet.create("处理时加入")
        return try_archive(agent_id)

    monkeypatch.setattr(fleet, "_try_archive", create_before_moving)
    assert [row["id"] for row in fleet.archive_all([first])["archived"]] == [first]
    assert [row["name"] for row in fleet.list()] == ["处理时加入"]


def test_archive_registration_failure_does_not_move_or_close_agent(tmp_path, monkeypatch):
    fleet = AgentManager(tmp_path)
    agent = fleet.get(fleet.create("仍然在舰队")["id"])

    def fail_save(reg):
        raise OSError("registry temporarily read-only")

    monkeypatch.setattr(fleet, "_save_registry", fail_save)
    result = fleet.archive_all([agent.fleet_id])
    assert result["archived"] == [] and result["skipped"][0]["error"] == "archive_write_failed"
    assert fleet.get(agent.fleet_id) is agent and not agent.dialogue_closed
    assert agent.data_dir.is_dir() and not list((tmp_path / "archive").iterdir())


@pytest.mark.parametrize("legacy", [False, True])
def test_empty_fleet_survives_bootstrap_without_recreating_or_readopting(tmp_path, legacy):
    data_root = tmp_path / "data"
    if legacy:
        ProtoAgent(data_root)
    fleet = _open_fleet(data_root, None, None, None)
    agent = fleet.get(fleet.current_id())
    agent_id = agent.fleet_id
    marker = agent.data_dir / "preserve.txt"
    marker.write_text("do not move the fleet root", encoding="utf-8")

    result = fleet.archive_all([agent_id])["archived"][0]
    reopened = _open_fleet(data_root, None, None, None)
    assert reopened.initialized and reopened.fleet_view() == {"current": None, "agents": []}
    if legacy:
        assert result["note"] == "external_directory"
        assert marker.read_text() == "do not move the fleet root"
        assert (data_root / "owner.key").is_file()
        assert reopened.root == data_root / ".fleet"
        assert reopened.adopt(agent) is None
    else:
        assert result["storage"] == "archive"
        assert not marker.exists()
    assert reopened.pause_all(False) == {"total": 0, "changed": 0}


def test_archive_records_retirement_even_if_managed_directory_cannot_move(tmp_path, monkeypatch):
    fleet = _open_fleet(tmp_path, None, None, None)
    agent = fleet.get(fleet.current_id())
    marker = agent.data_dir / "pin.txt"
    marker.write_text("unswapped data", encoding="utf-8")
    rename = Path.rename

    def refuse_archive(src, target):
        if src == agent.data_dir:
            raise OSError("same-device rename unavailable")
        return rename(src, target)

    monkeypatch.setattr(Path, "rename", refuse_archive)
    archived = fleet.archive_all([agent.fleet_id])["archived"]
    assert archived[0]["storage"] == "in_place" and archived[0]["note"] == "archive_move_failed"
    registry = json.loads((tmp_path / "registry.json").read_text())
    assert not (tmp_path / registry["archived"][0]["archive_dir"]).exists()
    assert marker.read_text() == "unswapped data"
    assert _open_fleet(tmp_path, None, None, None).list() == []


def test_console_bulk_confirmation_and_partial_feedback():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    html = (Path(__file__).resolve().parents[1] / "proto/web/index.html").read_text()
    marker = "let fleetBulkPending = false;"
    script = marker + html.split(marker, 1)[1].split("async function refreshFleet", 1)[0]
    harness = '''
const assert = require('node:assert/strict');
const elements = {};
const get = id => elements[id] ??= {disabled: true, textContent: ''};
const document = {getElementById: get};
const first = {id: 'first', name: '空闲', paused: false};
const second = {id: 'second', name: '忙碌', paused: true};
let fleetMembers = [first, second];
let approve = false;
let refreshes = 0;
let next;
const calls = [];
function confirm(prompt) { assert.match(prompt, /归档当前 2 个/); return approve; }
function api(method, path, fields) { calls.push([method, path, fields]); return next(); }
async function refreshFleet() { refreshes++; }
'''
    assertions = r'''
(async () => {
  syncFleetBulk();
  assert.equal(get('fleet-bulk-pause').disabled, false);
  assert.equal(get('fleet-bulk-resume').disabled, false);
  await changeFleet('archive');
  assert.equal(calls.length, 0);
  approve = true;
  let finish;
  next = () => new Promise(resolve => { finish = resolve; });
  const pending = changeFleet('archive');
  const repeat = changeFleet('archive');
  assert.equal(calls.length, 1);
  await repeat;
  assert.equal(get('fleet-bulk-resume').disabled, true);
  assert.deepEqual(calls[0], ['POST', '/api/agents/bulk',
    {action: 'archive', confirmed: true, agent_ids: ['first', 'second']}]);
  finish({total: 2, archived: [first], skipped: [{...second, error: 'chat_in_progress'}]});
  await pending;
  const status = get('fleet-bulk-status').textContent;
  assert.match(status, /已归档 1\/2/);
  assert.match(status, /忙碌（second）：正在回复主人/);
  assert.equal(refreshes, 1);
  next = () => { throw new Error('fleet_membership_changed'); };
  await changeFleet('archive');
  assert.match(get('fleet-bulk-status').textContent, /本次未归档/);
  assert.equal(refreshes, 2);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    subprocess.run([node], input=harness + script + assertions, text=True, check=True, timeout=8)
