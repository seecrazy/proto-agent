import io
import json
import socket
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from proto.exec.agentic import LOOP_SYSTEM
from proto.llm import breaker as model_breaker
from proto.llm.breaker import model_wait_reason
from proto.llm.catalog import ModelCatalog, ModelConfigError
from proto.llm.client import LLMError, ModelPressureWait
from proto.run import model_command
from proto.runtime import runs as dispatcher
from proto.runtime.manager import AgentManager
from proto.system.execution_log import execution_logging
from tests.test_m64_live_models import profile, save, select
from tests.test_m66_fleet_runs import console, request
from tests.test_m71_continuity import DONE, PASS, WRITE, record_writes, start_autonomous, step


class FakeProvider:
    def __init__(self):
        self.received = []
        self.responses = {}

    @property
    def models(self):
        return [body["model"] for body in self.received]

    def send(self, request, **options):
        body = json.loads(request.data)
        self.received.append(body)
        model = body["model"]
        result = self.responses.get(model, model)
        if callable(result):
            result = result(body)
        if isinstance(result, Exception):
            raise result
        if type(result) is int or isinstance(result, tuple):
            status, hint = result if isinstance(result, tuple) else (result, None)
            raise HTTPError(request.full_url, status, "private provider detail",
                            {"Retry-After": hint} if hint else {}, io.BytesIO(b"private provider response"))
        if result is None:
            return io.BytesIO(b"{}")
        if request.full_url.endswith("/messages"):
            payload = {"content": [{"type": "text", "text": result}],
                       "usage": {"input_tokens": 31, "output_tokens": 3}}
        else:
            payload = {"choices": [{"message": {"content": result}}],
                       "usage": {"prompt_tokens": 31, "completion_tokens": 3}}
        return io.BytesIO(json.dumps(payload).encode())


@pytest.fixture()
def provider(monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr("urllib.request.urlopen", fake.send)
    return fake


def save_group(catalog, members, *, name="日常编码", identity=None):
    group = {"name": name, "profile_ids": list(members)}
    if identity is not None:
        group["id"] = identity
    state = catalog.save_group(group, catalog.view()["revision"])
    return "group:" + state["saved_group_id"]


def configured(root, *rows):
    catalog = ModelCatalog(root)
    for row in rows:
        if isinstance(row, str):
            row = profile(row, f"https://{row}.invalid/v1")
        save(catalog, row)
    target = save_group(catalog, [row["id"] for row in catalog.view()["profiles"]])
    select(catalog, target)
    fleet = AgentManager(root, models=catalog)
    return fleet, fleet.get(fleet.create("工程师")["id"]), target


def complete(agent, *, fast=False, foreground=False):
    return agent._complete(agent.llm_fast if fast else agent.llm, "system", "user text", foreground=foreground)


def test_legacy_catalog_and_group_choice_survive_restart(tmp_path, provider, capsys):
    catalog = ModelCatalog(tmp_path)
    save(catalog, profile("large", "https://large.invalid/v1"))
    save(catalog, profile("flash", "https://flash.invalid/v1"))
    select(catalog, "large", "flash")
    assert "groups" not in json.loads(catalog.path.read_text())
    catalog = ModelCatalog(tmp_path)
    assert catalog.view()["groups"] == []
    target = save_group(catalog, ["flash", "large"])
    assert catalog.view()["selection"] == {"main": "large", "fast": "flash"}
    fast_target = save_group(catalog, ["large"], name="验收")
    model_command(catalog, f"/model {target} {fast_target}")
    printed = capsys.readouterr().out
    assert target in printed and "flash → large" in printed and "test-private-key" not in printed
    assert not provider.received and stat.S_IMODE(catalog.path.stat().st_mode) == 0o600
    reopened = ModelCatalog(tmp_path)
    fleet = AgentManager(tmp_path, models=reopened)
    agent = fleet.get(fleet.create("主快分流")["id"])
    assert "日常编码" in agent.llm.describe() and "验收" in agent.llm_fast.describe()
    assert complete(agent) == "flash" and complete(agent, fast=True) == "large"
    select(reopened, target)
    assert complete(agent, fast=True) == "flash"
    select(reopened, "large", fast_target)
    assert complete(agent) == "large" and complete(agent, fast=True) == "large"
    assert "test-private-key" not in json.dumps(reopened.view())


@pytest.mark.parametrize("group", [
    {"name": "empty", "profile_ids": []}, {"name": "dup", "profile_ids": ["alpha", "alpha"]},
    {"name": "missing", "profile_ids": ["missing"]}, {"name": "nested", "profile_ids": ["group:x"]},
    {"name": "bad shape", "profile_ids": "alpha"}, {"name": "forbidden field", "profile_ids": ["alpha"], "api_key": "key"},
    {"name": "excess", "profile_ids": ["alpha"] * 9}, {"name": "", "profile_ids": ["alpha"]},
    {"id": "missing", "name": "old", "profile_ids": ["alpha"]},
])
def test_invalid_group_does_not_modify_catalog(tmp_path, group):
    catalog = ModelCatalog(tmp_path)
    before = save(catalog, profile())
    data = catalog.path.read_bytes()
    with pytest.raises(ModelConfigError):
        catalog.save_group(group, before["revision"])
    assert catalog.path.read_bytes() == data and not catalog.view()["groups"]


def test_group_reload_rejects_incomplete_directory(tmp_path):
    fleet, _, target = configured(tmp_path, "alpha", "beta")
    catalog = fleet.models
    document = json.loads(catalog.path.read_bytes())
    document["profiles"].pop()
    catalog.path.write_text(json.dumps(document), encoding="utf-8")
    before = catalog.view()
    with pytest.raises(ModelConfigError, match="unknown_model_group_member"):
        catalog.reload(before["revision"])
    assert catalog.view() == before and before["selection"]["main"] == target


def test_web_create_edit_and_select_groups(tmp_path):
    fleet = AgentManager(tmp_path)
    catalog = fleet.enable_model_updates()
    save(catalog, profile())
    save(catalog, profile("beta"))
    group_command = {"expected_revision": 2, "group": {"name": "编程组", "profile_ids": ["alpha", "beta"]}}
    with console(fleet) as server:
        assert request(server, "/api/models/groups", group_command, token="wrong")[0] == 401
        status, created = request(server, "/api/models/groups", group_command)
        assert status == 200 and created["selection"] == {"main": None, "fast": None}
        identity = created["saved_group_id"]
        assert request(server, "/api/models/groups", group_command)[0] == 409
        status, invalid = request(server, "/api/models/select", {"expected_revision": 3, "main": "group:bad", "fast": None})
        assert status == 400 and invalid["error"] == "unknown_model"
        status, selected = request(server, "/api/models/select", {"expected_revision": 3, "main": f"group:{identity}", "fast": None})
        assert status == 200 and selected["selection"]["main"] == f"group:{identity}"
        status, edited = request(server, "/api/models/groups", {"expected_revision": 4,
            "group": {"id": identity, "name": "新编程组", "profile_ids": ["beta", "alpha"]}})
        assert status == 200 and edited["selection"] == selected["selection"]
        status, state = request(server, "/api/models")
        assert status == 200 and "test-private-key" not in json.dumps(state)
        assert state["groups"] == [{"id": identity, "name": "新编程组", "profile_ids": ["beta", "alpha"]}]


def test_web_execution_mode_is_validated_hot_applied_and_persisted(tmp_path):
    fleet = AgentManager(tmp_path)
    catalog = fleet.enable_model_updates()
    save(catalog, profile())
    catalog.select("alpha", None, 1)
    payload = {"expected_revision": 2, "profile": {"id": "alpha", "execution_mode": "unknown"}}
    with console(fleet) as server:
        assert request(server, "/api/models", payload)[0] == 400
        payload["profile"]["execution_mode"] = "native"
        status, state = request(server, "/api/models", payload)
        assert status == 200 and state["profiles"][0]["execution_mode"] == "native"
        assert catalog.pin("main").native_decisions and ModelCatalog(tmp_path).pin("main").native_decisions
        payload["expected_revision"] = state["revision"]
        payload["profile"]["execution_mode"] = "auto"
        assert request(server, "/api/models", payload)[0] == 200
        assert not catalog.pin("main").native_decisions


@pytest.mark.parametrize("failure", [401, 404, 429, 503, TimeoutError("private"),
                                     URLError("offline"), ConnectionResetError("closed")])
def test_recoverable_failure_fails_over_and_settles_each_sent_call(tmp_path, provider, failure):
    fleet, agent, _ = configured(tmp_path, "primary", profile("fallback", "https://backup.invalid", style="anthropic"))
    provider.responses["primary"] = failure
    own = agent.kernel.pools.remaining_all()["global"]
    assert complete(agent) == "fallback"
    assert provider.models == ["primary", "fallback"]
    assert not agent.model_status()["waiting"]
    usage = fleet.runs.budget.view()
    estimated = (len("system") + len("user text")) // 4
    assert usage["used"] == {"calls": 2, "tokens": estimated + 34}
    assert usage["in_flight"] == 0 and usage["uncertain"] == 1
    remaining = agent.kernel.pools.remaining_all()["global"]
    assert own["calls"] - remaining["calls"] == 2 and own["tokens"] - remaining["tokens"] == estimated + 34
    assert complete(agent) == "fallback" and provider.models == ["primary", "fallback", "fallback"]


def test_group_attempt_cap_keeps_third_candidate_for_next_call(tmp_path, provider):
    fleet, agent, _ = configured(tmp_path, "first", "second", "third")
    provider.responses.update(first=503, second=503)
    with pytest.raises(ModelPressureWait, match="model_group_unavailable") as waited:
        complete(agent)
    assert waited.value.retry_at is None and provider.models == ["first", "second"]
    assert fleet.runs.budget.view()["used"]["calls"] == 2
    assert not agent.model_status()["waiting"] and complete(agent) == "third"


@pytest.mark.parametrize("failure,reason", [(400, "model_http_error"), (403, "model_forbidden"),
                                          (422, "model_http_error"), (None, "model_request_failed")])
def test_request_rejections_never_switch_even_after_breaker_trips(tmp_path, provider, failure, reason):
    _, agent, _ = configured(tmp_path, "primary", "fallback")
    provider.responses["primary"] = failure
    for _index in range(4):
        with pytest.raises(LLMError) as refused:
            complete(agent)
        assert model_wait_reason(refused.value) == reason
    assert provider.models == ["primary"] * (1 if failure == 403 else 3)
    assert agent.model_status()["waiting"] and agent.model_status()["reason"] == reason


@pytest.mark.parametrize("scope", ["fleet", "agent", "cancel"])
def test_retry_respects_budgets_and_cancellation(tmp_path, provider, scope, monkeypatch):
    fleet, agent, _ = configured(tmp_path, "primary", "fallback")
    provider.responses["primary"] = 503
    if scope == "fleet":
        fleet.runs.budget.configure(10000, 1)
    elif scope == "agent":
        agent.kernel.pools.set_cap("global", "calls", 1)
    else:
        monkeypatch.setattr(agent, "_blocked_dispatch", lambda _autonomous: "run_cancelled" if provider.received else "")
    with pytest.raises(LLMError, match="budget_exhausted|run_cancelled"):
        complete(agent)
    assert provider.models == ["primary"]
    assert fleet.runs.budget.view()["used"]["calls"] == 1 and fleet.runs.budget.view()["in_flight"] == 0


def test_busy_group_origin_skips_alias_without_charging(tmp_path, provider):
    fleet, agent, _ = configured(tmp_path, profile("primary", "https://same.invalid/one", api_key="key-one"),
        profile("alias", "https://same.invalid:443/two", api_key="key-two"), "other")
    primary, alias, other = agent.llm.plan().members
    pressure = fleet.runs.model_pressure
    assert primary.pressure_key != alias.pressure_key and primary.pressure_origin == alias.pressure_origin
    with pressure.admit(primary.pressure_key, origin=primary.pressure_origin):
        assert complete(agent, foreground=True) == "other"
        with pressure.admit(other.pressure_key, origin=other.pressure_origin):
            with pytest.raises(ModelPressureWait, match="model_route_busy"):
                complete(agent)
            assert agent.model_status()["waiting"]
    assert provider.models == ["other"] and fleet.runs.budget.view()["used"]["calls"] == 1


def test_origin_cooldowns_survive_agent_role_and_restart_and_choose_earliest(tmp_path, provider):
    fleet, first, _ = configured(tmp_path, profile("primary", "https://same.invalid/one", api_key="key-one"),
        profile("alias", "https://same.invalid:443/two", api_key="key-two"), "other")
    second = fleet.get(fleet.create("分析师")["id"])
    clock = [time.time()]
    fleet.runs.model_pressure.now = lambda: clock[0]
    provider.responses.update(primary=(429, "120"), other=(429, "30"))
    with pytest.raises(ModelPressureWait, match="model_group_unavailable") as blocked:
        complete(first)
    assert blocked.value.retry_at == clock[0] + 30 and provider.models == ["primary", "other"]
    assert second.model_status()["retry_at"] == blocked.value.retry_at
    with pytest.raises(ModelPressureWait):
        complete(second, fast=True)
    reopened = AgentManager(tmp_path, models=ModelCatalog(tmp_path))
    reopened.runs.model_pressure.now = lambda: clock[0]
    recovered = reopened.get(second.fleet_id)
    with pytest.raises(ModelPressureWait):
        complete(recovered)
    assert len(provider.received) == 2 and reopened.runs.budget.view()["used"]["calls"] == 2
    clock[0] += 30
    provider.responses["other"] = "backup-online"
    assert complete(recovered) == "backup-online"
    clock[0] += 90
    provider.responses["primary"] = "primary-online"
    assert complete(recovered, fast=True) == "primary-online"
    assert provider.models == ["primary", "other", "other", "primary"]


def test_inflight_group_keeps_members_and_credentials_after_edit(tmp_path, provider):
    fleet, agent, group_target = configured(tmp_path, "primary", "fallback", "next")
    entered, release = threading.Event(), threading.Event()

    def primary(_body):
        entered.set()
        assert release.wait(4)
        return 503

    provider.responses["primary"] = primary
    with ThreadPoolExecutor(max_workers=1) as worker:
        inflight = worker.submit(complete, agent)
        try:
            assert entered.wait(4)
            save(fleet.models, {"id": "fallback", "model": "different"})
            save_group(fleet.models, ["next"], identity=group_target.removeprefix("group:"))
        finally:
            release.set()
        assert inflight.result(timeout=4) == "fallback"
    assert complete(agent) == "next" and provider.models == ["primary", "fallback", "next"]


@pytest.mark.parametrize("already_wrote", [False, True])
@pytest.mark.parametrize("failure_mode", ["rate_limit", "read_timeout"])
def test_unavailable_group_requeues_same_run_and_does_not_replay_tools(
        tmp_path, provider, monkeypatch, already_wrote, failure_mode):
    fleet, agent, _ = configured(tmp_path, "primary", "fallback")
    clock = [time.time()]
    monkeypatch.setattr(dispatcher, "time", SimpleNamespace(time=lambda: clock[0], monotonic=time.monotonic))
    fleet.runs.model_pressure.now = lambda: clock[0]
    if failure_mode == "read_timeout":
        monkeypatch.setattr(model_breaker, "time", SimpleNamespace(time=lambda: clock[0]))
        first_failure, second_failure = socket.timeout("private timeout"), socket.timeout("private timeout")
        delay = 600
    else:
        first_failure, second_failure = (429, "90"), (429, "60")
        delay = 60
    initial = iter([json.dumps(WRITE), first_failure] if already_wrote else [first_failure])
    provider.responses.update(primary=lambda body: next(initial) if body["messages"][0]["content"] == LOOP_SYSTEM
                              else "检查输入并核验", fallback=second_failure)
    agent.configure_autonomy(enabled=True, mode="auto")
    run, _ = start_autonomous(fleet, agent)
    effects = tmp_path / "effects.txt"
    record_writes(agent, effects)
    with execution_logging(tmp_path, "web"):
        parked = step(fleet)
    assert parked["status"] == "queued" and parked["reason"] == "model_retry_wait"
    assert parked["result"]["model_reason"] == "model_group_unavailable"
    assert parked["result"]["retry_at"] == clock[0] + delay
    events = fleet.runs.activity.snapshot(agent)["events"]
    assert [event["model"] for event in events if event["phase"] == "model_failed"] == ["primary", "fallback"]
    assert "private provider" not in json.dumps(events) and "test-private-key" not in json.dumps(events)
    assert fleet.runs._pick() is None and effects.exists() is already_wrote

    def resume(body):
        if body["messages"][0]["content"] != LOOP_SYSTEM:
            return json.dumps(PASS)
        return json.dumps(DONE)

    provider.responses["fallback"] = resume
    if failure_mode == "read_timeout":
        provider.responses["primary"] = socket.timeout("private timeout")
    reopened = AgentManager(tmp_path, models=ModelCatalog(tmp_path))
    reopened.runs.model_pressure.now = lambda: clock[0]
    record_writes(reopened.get(agent.fleet_id), effects)
    assert reopened.runs._pick() is None
    clock[0] += delay
    with execution_logging(tmp_path, "web"):
        finished = step(reopened)
    assert finished["status"] == "succeeded" and finished["id"] == run["id"]
    assert finished["session_id"] == parked["session_id"]
    assert provider.models[-1] == "fallback"
    if failure_mode == "rate_limit":
        assert provider.models[-2:] == ["fallback", "fallback"]
    assert (effects.read_text(encoding="utf-8") if effects.exists() else "") == ("写入\n" if already_wrote else "")
    history = (tmp_path / ".runtime-logs/execution.jsonl").read_text(encoding="utf-8")
    rows = [json.loads(line) for line in history.splitlines()]
    starts = [row for row in rows if row["event"] == "run_started"]
    finishes = [row for row in rows if row["event"] == "run_finished"]
    assert [(row["run_id"], row["generation"], row["resumed"]) for row in starts] == [
        (run["id"], 1, False), (run["id"], 2, True)]
    assert len({row["boot_id"] for row in starts}) == 2
    assert [row["status"] for row in finishes] == ["queued", "succeeded"]
    assert finishes[0]["reason"] == "model_retry_wait" and finishes[0]["retry_at"] == parked["result"]["retry_at"]
    assert finishes[0]["model_reason"] == "model_group_unavailable" and finishes[0]["model_retries"] == 1
    model_failures = [row for row in rows if row["event"] == "model_failed"]
    assert model_failures and all(row["run_id"] == run["id"] for row in model_failures)
    assert "private provider" not in history and "private timeout" not in history and "test-private-key" not in history
