# -*- coding: utf-8 -*-
import copy
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from proto.exec.agentic import AgenticLoop
from proto.exec.contracts import object_schema
from proto.kernel.anchor import Channel
from proto.kernel.kernel import ActionCategory
from proto.llm.catalog import ModelCatalog, ModelConfigError, ModelRevisionConflict
from proto.llm.client import AnthropicCompatClient, LLMError, OpenAICompatClient
from proto.runtime.agent import ProtoAgent
from proto.runtime.manager import AgentManager
from proto.web.server import ProtoWebServer


def profile(identity="alpha", base="http://127.0.0.1:9/v1", style="openai", **changes):
    result = {"id": identity, "name": identity, "api_style": style,
              "base_url": base, "model": identity, "api_key": "test-private-key"}
    result.update(changes)
    return result


def save(catalog, value):
    return catalog.save_profile(value, catalog.view()["revision"])


def select(catalog, main, fast=None):
    return catalog.select(main, fast, catalog.view()["revision"])


@pytest.fixture()
def endpoint():
    received = []
    entered = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *arguments):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append({"body": body, "path": self.path, "headers": dict(self.headers)})
            model = body["model"]
            if model == "blocked":
                entered.set()
                if not release.wait(10):
                    self.send_error(504)
                    return
            if model == "broken":
                self.send_error(503)
                return
            system = body.get("system") or body["messages"][0]["content"]
            if "归因" in system:
                reply = "[]"
            elif "任务规划器" in system:
                reply = json.dumps([{"tool": "clock", "args": {}, "category": "read_draft"}])
            elif "独立验收" in system:
                reply = json.dumps({"verdict": "pass", "missing": []})
            elif "任务执行循环" in system:
                reply = json.dumps({"tool": "external", "args": {"value": "once"}}
                                   if model == "pause" else {"done": True, "summary": "完成"})
            else:
                reply = model
            tokens = 11 if model == "blocked" else 31
            if self.path.endswith("/messages"):
                result = {"content": [{"type": "text", "text": reply}],
                          "usage": {"input_tokens": tokens, "output_tokens": 3}}
            else:
                result = {"choices": [{"message": {"content": reply}}],
                          "usage": {"prompt_tokens": tokens, "completion_tokens": 3}}
            data = json.dumps(result).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f"http://127.0.0.1:{server.server_port}", received, entered, release
    release.set()
    server.shutdown()
    server.server_close()
    worker.join(2)


@pytest.fixture()
def web(tmp_path):
    agent = ProtoAgent(tmp_path / "data", goal_interval_s=10**9)
    server = ProtoWebServer(agent, port=0, heartbeat_s=0)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield server, agent, server.manager.models
    server.shutdown()
    server.server_close()
    worker.join(2)


def request(server, method, path, body=None, authenticated=True):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if authenticated:
        headers["X-Proto-Token"] = server.token
    command = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}",
                                     data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(command, timeout=3) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


@pytest.mark.parametrize("style", ["openai", "anthropic"])
def test_new_and_existing_models_switch_without_restart(tmp_path, endpoint, style):
    base, received, _, _ = endpoint
    catalog = ModelCatalog(tmp_path)
    assert catalog.client("main") is None
    save(catalog, profile(base=base + "/api", style=style))
    assert catalog.client("main") is None
    select(catalog, "alpha")
    role = catalog.client("main")
    assert role.complete("system", "user") == "alpha"
    assert role.last_usage == {"input_tokens": 31, "output_tokens": 3}
    save(catalog, profile("beta", base + "/api", style))
    assert role.complete("system", "user") == "alpha"
    select(catalog, "beta", "alpha")
    assert role is catalog.client("main")
    assert role.complete("system", "user") == "beta"
    assert catalog.client("fast").complete("system", "user") == "alpha"
    select(catalog, "beta")
    assert catalog.client("fast").complete("system", "user") == "beta"
    assert received[-1]["path"].endswith("/v1/messages" if style == "anthropic" else "/chat/completions")
    if style == "anthropic":
        assert received[-1]["headers"]["X-Api-Key"] == "test-private-key"
    else:
        assert received[-1]["headers"]["Authorization"] == "Bearer test-private-key"


def test_active_profile_edits_and_key_actions(tmp_path, endpoint):
    base, received, _, _ = endpoint
    catalog = ModelCatalog(tmp_path)
    save(catalog, profile(base=base + "/one"))
    select(catalog, "alpha")
    client = catalog.client("main")
    save(catalog, {"id": "alpha", "base_url": base + "/two", "model": "updated",
                   "api_style": "anthropic", "api_key": "replacement"})
    assert client.complete("system", "user") == "updated"
    assert received[-1]["path"] == "/two/v1/messages"
    assert received[-1]["headers"]["X-Api-Key"] == "replacement"
    save(catalog, {"id": "alpha", "name": "new name", "api_key": ""})
    assert client.complete("system", "user") == "updated"
    assert received[-1]["headers"]["X-Api-Key"] == "replacement"
    save(catalog, {"id": "alpha", "clear_api_key": True})
    client.complete("system", "user")
    assert received[-1]["headers"]["X-Api-Key"] == ""


def test_persistence_bootstrap_and_private_view(tmp_path):
    main = OpenAICompatClient("http://localhost:1234/v1", "private-main", "main")
    fast = AnthropicCompatClient("http://localhost:1234/anthropic", "private-fast", "fast")
    catalog = ModelCatalog(tmp_path, main, fast)
    assert catalog.view()["selection"] == {"main": "env-main", "fast": "env-fast"}
    select(catalog, "env-fast")
    assert os.stat(catalog.path).st_mode & 0o777 == 0o600
    catalog.path.chmod(0o644)
    restarted = ModelCatalog(tmp_path, main, fast)
    assert os.stat(catalog.path).st_mode & 0o777 == 0o600
    assert restarted.view()["selection"] == {"main": "env-fast", "fast": None}
    assert "private-main" not in json.dumps(restarted.view())
    assert "private-fast" not in json.dumps(restarted.view())
    assert all("api_key" not in row and row["has_api_key"] for row in restarted.view()["profiles"])
    view = restarted.view()
    view["profiles"][0]["model"] = "tampered"
    assert restarted.view()["profiles"][0]["model"] == "main"


@pytest.mark.parametrize("changes", [
    {"id": False}, {"id": ""}, {"id": "../escape"}, {"model": ""}, {"model": 3},
    {"api_style": "unknown"}, {"name": None}, {"api_key": ["private"]},
    {"base_url": "file:///tmp/key"}, {"base_url": "https://user:private@host/v1"},
    {"base_url": "https://host/v1?key=private"}, {"base_url": "https://host/v1#private"},
    {"base_url": "http://host:70000/v1"}, {"base_url": "http://host:0/v1"},
    {"base_url": "http://host/a b"}, {"base_url": "http://host/\nprivate"},
    {"clear_api_key": "false"}, {"clear_api_key": True}, {"extra": "private"},
    {"api_key": "\ud800"}, {"model": "\udfff"},
])
def test_invalid_profiles_are_atomic_and_do_not_echo_values(tmp_path, changes):
    catalog = ModelCatalog(tmp_path)
    before = catalog.view()
    with pytest.raises(ModelConfigError) as error:
        save(catalog, profile(**changes))
    assert "private" not in str(error.value)
    assert catalog.view() == before
    assert not catalog.path.exists()


@pytest.mark.parametrize("revision", [None, "0", True, -1, 0.0])
def test_revision_must_be_real_nonnegative_integer(tmp_path, revision):
    catalog = ModelCatalog(tmp_path)
    with pytest.raises(ModelConfigError):
        catalog.save_profile(profile(), revision)
    assert not catalog.path.exists()


def test_concurrent_edit_conflict_keeps_single_commit(tmp_path):
    catalog = ModelCatalog(tmp_path)
    barrier = threading.Barrier(2)

    def update(identity):
        barrier.wait(3)
        try:
            catalog.save_profile(profile(identity), 0)
            return "saved"
        except ModelRevisionConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(update, ["alpha", "beta"])) == ["conflict", "saved"]
    assert len(catalog.view()["profiles"]) == 1
    assert json.loads(catalog.path.read_text())["revision"] == 1


def test_write_failure_preserves_live_and_disk_state(tmp_path, monkeypatch):
    catalog = ModelCatalog(tmp_path)
    save(catalog, profile())
    select(catalog, "alpha")
    before = catalog.view()
    persisted = catalog.path.read_bytes()
    binding = catalog.client("main").pin().binding_key

    def fail(state):
        raise ModelConfigError("model_catalog_write_failed")

    monkeypatch.setattr(catalog, "_write", fail)
    with pytest.raises(ModelConfigError):
        save(catalog, {"id": "alpha", "model": "not-published"})
    assert catalog.view() == before
    assert catalog.path.read_bytes() == persisted
    assert catalog.client("main").pin().binding_key == binding


def test_atomic_replace_failure_leaves_no_temporary_file(tmp_path, monkeypatch):
    catalog = ModelCatalog(tmp_path)
    save(catalog, profile())
    before = catalog.view()
    persisted = catalog.path.read_bytes()

    def fail(*arguments):
        raise OSError("private-path-not-for-api")

    monkeypatch.setattr("proto.llm.catalog.os.replace", fail)
    with pytest.raises(ModelConfigError, match="model_catalog_write_failed"):
        save(catalog, {"id": "alpha", "model": "not-published"})
    assert catalog.view() == before and catalog.path.read_bytes() == persisted
    assert list(tmp_path.glob(".llm-models-*")) == []


def test_serialized_size_limit_prevents_unreadable_catalog(tmp_path):
    catalog = ModelCatalog(tmp_path)
    candidate = copy.deepcopy(catalog._state)
    candidate["profiles"] = [profile(f"model-{index}", api_key="🔑" * 4096) for index in range(64)]
    with pytest.raises(ModelConfigError, match="model_catalog_too_large"):
        catalog._publish(candidate)
    assert catalog.view()["revision"] == 0 and not catalog.path.exists()


def test_main_fast_selection_is_published_as_one_snapshot(tmp_path):
    catalog = ModelCatalog(tmp_path)
    save(catalog, profile())
    save(catalog, profile("beta"))
    select(catalog, "alpha", "beta")
    barrier = threading.Barrier(2)

    def update():
        barrier.wait(3)
        for index in range(40):
            select(catalog, "alpha" if index % 2 else "beta", "beta" if index % 2 else "alpha")

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(update)
        barrier.wait(3)
        for _ in range(1000):
            selection = catalog.view()["selection"]
            assert (selection["main"], selection["fast"]) in (("alpha", "beta"), ("beta", "alpha"))
        pending.result(5)


def test_custom_injection_retains_identity_and_disabled_fast_until_selection(tmp_path, endpoint):
    base, _, _, _ = endpoint

    class Injected:
        def __init__(self):
            self.calls = []

        def complete(self, system, user):
            self.calls.append(user)
            return "injected"

    injected = Injected()
    agent = ProtoAgent(tmp_path / "agent", llm=injected)
    agent.llm_fast = None
    original_attributor = agent.attributor
    original_planner = agent.planner
    catalog = ModelCatalog(tmp_path, injected)
    agent.bind_model_catalog(catalog)
    assert agent.llm is injected and agent.llm_fast is None and agent.planner is original_planner
    assert agent.attributor is original_attributor
    save(catalog, profile(base=base))
    assert agent.llm is injected and agent.llm.calls == []
    select(catalog, "alpha")
    assert agent._complete(agent.llm, "s", "u") == "alpha"
    assert agent.planner is not original_planner and injected.calls == []


@pytest.mark.parametrize("damage", ["json", "version", "duplicate", "unknown", "oversize", "symlink"])
def test_bad_reload_preserves_running_selection(tmp_path, damage):
    catalog = ModelCatalog(tmp_path)
    save(catalog, profile())
    select(catalog, "alpha")
    before = catalog.view()
    document = json.loads(catalog.path.read_text())
    if damage == "json":
        catalog.path.write_text("{invalid")
    elif damage == "oversize":
        catalog.path.write_bytes(b" " * (1024 * 1024 + 1))
    elif damage == "symlink":
        other = tmp_path / "external.json"
        catalog.path.rename(other)
        catalog.path.symlink_to(other)
    else:
        if damage == "version":
            document["schema_version"] = 2
        elif damage == "duplicate":
            document["profiles"].append(document["profiles"][0])
        else:
            document["selection"]["fast"] = "missing"
        catalog.path.write_text(json.dumps(document))
    with pytest.raises(ModelConfigError):
        catalog.reload(before["revision"])
    assert catalog.view() == before


def test_disk_new_models_and_selection_reload_without_restart(tmp_path, endpoint):
    base, _, _, _ = endpoint
    catalog = ModelCatalog(tmp_path)
    save(catalog, profile(base=base))
    select(catalog, "alpha")
    document = json.loads(catalog.path.read_text())
    document["profiles"].append(profile("beta", base))
    document["selection"] = {"main": "beta", "fast": "alpha"}
    catalog.path.write_text(json.dumps(document))
    catalog.reload(catalog.view()["revision"])
    assert catalog.client("main").complete("s", "u") == "beta"
    assert catalog.client("fast").complete("s", "u") == "alpha"


def test_model_count_limit(tmp_path):
    catalog = ModelCatalog(tmp_path)
    document = copy.deepcopy(catalog._state)
    document["profiles"] = [profile(f"model-{index}") for index in range(65)]
    catalog.path.write_text(json.dumps(document))
    with pytest.raises(ModelConfigError, match="profiles"):
        catalog.reload(0)
    assert catalog.view()["profiles"] == []


def test_offline_cached_new_lazy_and_adopted_agents_share_models(tmp_path, endpoint):
    base, received, _, _ = endpoint
    catalog = ModelCatalog(tmp_path)
    manager = AgentManager(tmp_path / "fleet", models=catalog)
    first = manager.create("first")
    second = manager.create("second")
    adopted = ProtoAgent(tmp_path / "legacy")
    manager.adopt(adopted)
    cached = manager.get(first["id"])
    assert cached.llm is None and cached.planner is None
    save(catalog, profile(base=base))
    save(catalog, profile("fast", base))
    select(catalog, "alpha", "fast")
    manager._cache.pop(second["id"])
    third = manager.create("third")
    for agent in (cached, manager.get(second["id"]), manager.get(third["id"]), adopted):
        assert agent._complete(agent.llm, "s", "u") == "alpha"
        assert agent.planner.plan("read clock")[0]["tool"] == "clock"
        assert received[-1]["body"]["model"] == "fast"
        assert agent.attributor.attribute("test", Channel.OWNER) == []
        assert received[-1]["body"]["model"] == "alpha"


@pytest.mark.parametrize("same_credential", [True, False])
def test_inflight_call_keeps_result_usage_and_does_not_block_switch(web, endpoint, same_credential):
    server, agent, catalog = web
    base, _, entered, release = endpoint
    save(catalog, profile("blocked", base))
    save(catalog, profile("beta", base, api_key="test-private-key" if same_credential else "another-key"))
    select(catalog, "blocked")
    before = agent.kernel.pools.remaining("owner_tasks", "tokens")

    def run():
        with server.manager.ops_lock(server.manager.current_id()):
            return agent._complete(agent.llm, "s", "u")

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(run)
        try:
            assert entered.wait(3)
            status, state = request(server, "POST", "/api/models/select", {
                "expected_revision": catalog.view()["revision"], "main": "beta", "fast": None})
            assert status == 200 and state["selection"]["main"] == "beta"
            assert not pending.done()
            if same_credential:
                with pytest.raises(LLMError, match="model_route_busy"):
                    agent._complete(agent.llm, "s", "u")
            else:
                assert agent._complete(agent.llm, "s", "u") == "beta"
        finally:
            release.set()
        assert pending.result(3) == "blocked"
    if same_credential:
        assert agent._complete(agent.llm, "s", "u") == "beta"
    assert before - agent.kernel.pools.remaining("owner_tasks", "tokens") == 14 + 34


def test_role_usage_is_thread_local_across_switch(tmp_path, endpoint):
    base, _, entered, release = endpoint
    catalog = ModelCatalog(tmp_path)
    save(catalog, profile("blocked", base))
    save(catalog, profile("beta", base))
    select(catalog, "blocked")
    client = catalog.client("main")

    def run():
        return client.complete("s", "u"), client.last_usage

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(run)
        try:
            assert entered.wait(3)
            select(catalog, "beta")
            assert client.complete("s", "u") == "beta"
            assert client.last_usage == {"input_tokens": 31, "output_tokens": 3}
        finally:
            release.set()
        assert pending.result(3) == ("blocked", {"input_tokens": 11, "output_tokens": 3})
    assert client.last_usage == {"input_tokens": 31, "output_tokens": 3}


def test_switch_does_not_reset_sessions_budget_or_approval(web, endpoint):
    server, agent, catalog = web
    base, received, _, _ = endpoint
    save(catalog, profile("pause", base))
    save(catalog, profile("beta", base))
    select(catalog, "pause")
    effects = []
    agent.tools.register("external", lambda value: effects.append(value) or "ok",
                         category=ActionCategory.VISIBLE_WRITE, owner_only=True,
                         parameters=object_schema({"value": {"type": "string"}}, ["value"]))
    paused = AgenticLoop(agent, max_steps=5).run("original task", acceptance="fixed criterion")
    assert paused["status"] == "paused"
    previous = agent.sessions.lookup(paused["session_id"])
    budget = agent.kernel.pools.remaining_all()
    select(catalog, "beta")
    assert agent.sessions.lookup(paused["session_id"]) == previous
    assert agent.kernel.pools.remaining_all() == budget
    assert len(agent.queue.pending()) == 1
    result = agent.resume_task(paused["task_id"], True)
    assert result["status"] == "verified" and result["session_id"] == paused["session_id"]
    assert effects == ["once"]
    resumed = agent.sessions.lookup(result["session_id"])
    assert resumed["next_step"] > previous["next_step"]
    assert resumed["context"] == previous["context"]
    assert any(row["body"]["model"] == "beta" for row in received)


def test_breakers_are_per_agent_and_configuration(tmp_path, endpoint):
    base, received, _, _ = endpoint
    catalog = ModelCatalog(tmp_path)
    save(catalog, profile("broken", base))
    save(catalog, profile("beta", base))
    agent = ProtoAgent(tmp_path / "agent")
    agent.bind_model_catalog(catalog)
    select(catalog, "broken")
    for _ in range(3):
        with pytest.raises(LLMError):
            agent._complete(agent.llm, "s", "u")
    with pytest.raises(LLMError, match="circuit_breaker_open"):
        agent._complete(agent.llm, "s", "u")
    select(catalog, "beta")
    assert agent._complete(agent.llm, "s", "u") == "beta"
    select(catalog, "broken")
    calls = len(received)
    with pytest.raises(LLMError, match="circuit_breaker_open"):
        agent._complete(agent.llm, "s", "u")
    assert len(received) == calls
    other = ProtoAgent(tmp_path / "other")
    other.bind_model_catalog(catalog)
    with pytest.raises(LLMError, match="503"):
        other._complete(other.llm, "s", "u")
    save(catalog, {"id": "broken", "model": "repaired"})
    assert agent._complete(agent.llm, "s", "u") == "repaired"


@pytest.mark.parametrize("method,path", [("GET", "/api/models"), ("POST", "/api/models"),
                                         ("POST", "/api/models/select"), ("POST", "/api/models/reload")])
def test_all_model_routes_require_token(web, method, path):
    server, _, catalog = web
    status, _ = request(server, method, path, {} if method == "POST" else None, authenticated=False)
    assert status == 401 and catalog.view()["revision"] == 0


def test_api_registration_conflicts_and_safe_errors(web, endpoint):
    server, agent, catalog = web
    base, _, _, _ = endpoint
    status, state = request(server, "POST", "/api/models", {
        "expected_revision": 0, "profile": profile(base=base)})
    assert status == 200 and "test-private-key" not in json.dumps(state)
    assert agent.llm is None
    status, _ = request(server, "POST", "/api/models/select", {
        "expected_revision": 0, "main": "alpha", "fast": None})
    assert status == 409 and agent.llm is None
    status, _ = request(server, "POST", "/api/models/select", {
        "expected_revision": state["revision"], "main": "missing", "fast": None})
    assert status == 400 and agent.llm is None
    status, state = request(server, "POST", "/api/models/select", {
        "expected_revision": state["revision"], "main": "alpha", "fast": None})
    assert status == 200 and agent._complete(agent.llm, "s", "u") == "alpha"
    status, error = request(server, "POST", "/api/models", {
        "expected_revision": state["revision"], "profile": profile(base="https://user:test-private-key@host")})
    assert status == 400 and "test-private-key" not in json.dumps(error)
    status, listing = request(server, "GET", "/api/models")
    assert status == 200 and listing["selection"]["main"] == "alpha"
    assert "test-private-key" not in json.dumps(listing)


@pytest.mark.parametrize("path,body", [
    ("/api/models", {"profile": profile()}),
    ("/api/models", {"expected_revision": 0, "profile": profile(), "extra": True}),
    ("/api/models/select", {"expected_revision": 0, "main": "missing", "fast": False}),
    ("/api/models/reload", {"expected_revision": 0, "path": "/private/key"}),
])
def test_model_api_rejects_incomplete_or_extra_fields(web, path, body):
    server, _, catalog = web
    status, _ = request(server, "POST", path, body)
    assert status == 400 and catalog.view()["revision"] == 0


def test_model_reload_api_applies_only_valid_disk_changes(web):
    server, agent, catalog = web
    save(catalog, profile())
    select(catalog, "alpha")
    document = json.loads(catalog.path.read_text())
    document["profiles"].append(profile("beta"))
    document["selection"] = {"main": "beta", "fast": None}
    catalog.path.write_text(json.dumps(document))
    status, state = request(server, "POST", "/api/models/reload", {
        "expected_revision": catalog.view()["revision"]})
    assert status == 200 and state["selection"]["main"] == "beta"
    assert "beta @" in agent.llm.describe()
    catalog.path.write_text("{bad")
    status, _ = request(server, "POST", "/api/models/reload", {"expected_revision": state["revision"]})
    assert status == 400 and "beta @" in agent.llm.describe()


def test_fleet_cli_uses_persisted_model_selection(tmp_path, monkeypatch):
    from proto.agents.__main__ import _manager
    catalog = ModelCatalog(tmp_path)
    save(catalog, profile())
    select(catalog, "alpha")
    monkeypatch.setattr("proto.llm.client.create_llm_stack", lambda: None)
    manager = _manager(str(tmp_path))
    info = manager.create("cli-agent")
    assert "alpha @" in manager.get(info["id"]).llm.describe()


def test_terminal_commands_use_same_catalog(tmp_path, capsys):
    from proto.run import model_command
    catalog = ModelCatalog(tmp_path)
    save(catalog, profile())
    save(catalog, profile("beta"))
    model_command(catalog, "/model alpha beta")
    assert catalog.view()["selection"] == {"main": "alpha", "fast": "beta"}
    model_command(catalog, "/model beta main")
    assert catalog.view()["selection"] == {"main": "beta", "fast": None}
    model_command(catalog, "/models reload")
    model_command(catalog, "/models")
    model_command(catalog, "/model missing")
    output = capsys.readouterr().out
    assert "unknown_model" in output and "test-private-key" not in output


def test_model_panel_scripts_submit_revision_and_never_cache_keys():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is not installed")
    html = (Path(__file__).resolve().parents[1] / "proto/web/index.html").read_text()
    script = re.search(r"let modelCatalog = null;.*?(?=function addMsg)", html, re.S).group()
    harness = r'''
const assert = require("assert");
const elements = {};
for (const identity of ["model-key", "model-clear-key", "model-main", "model-fast", "model-profile",
                       "model-name", "model-style", "model-base", "model-id", "model-key-label", "model-message",
                       "model-group", "model-group-name", "model-group-add", "model-group-members"])
  elements[identity] = {value: "", textContent: "", innerHTML: "", checked: false};
elements["model-panel"] = {querySelectorAll: () => [], showModal() {}, close() {}};
const document = {getElementById: identity => elements[identity]};
const esc = value => value.replaceAll("<", "&lt;");
const calls = [];
let reject = false;
const state = {revision: 7, selection: {main: "alpha", fast: null},
               profiles: [{id: "alpha", name: "<private>", model: "m", api_style: "openai", base_url: "http://localhost", has_api_key: true},
                          {id: "beta", name: "二号", model: "b"}],
               groups: [{id: "team", name: "<编码组>", profile_ids: ["beta"]}]};
async function api(method, path, body) {
  calls.push({method, path, body});
  if (reject) throw new Error("model_revision_conflict");
  return JSON.parse(JSON.stringify(state));
}
async function refreshState() {}
'''
    assertions = r'''
(async () => {
  await openModels();
  assert.equal(elements["model-main"].value, "alpha");
  assert(elements["model-profile"].innerHTML.includes("&lt;private>"));
  elements["model-profile"].value = "alpha";
  fillModelProfile();
  assert.equal(elements["model-key"].value, "");
  assert(elements["model-key-label"].textContent.includes("留空保留"));
  elements["model-key"].value = "fresh-secret";
  await saveModelProfile();
  assert.equal(calls.at(-1).body.profile.api_key, "fresh-secret");
  assert.equal(calls.at(-1).body.expected_revision, 7);
  assert.equal(elements["model-key"].value, "");
  assert(!JSON.stringify(modelCatalog).includes("fresh-secret"));
  await selectModels();
  assert.equal(calls.at(-1).path, "/api/models/select");
  assert.equal(calls.at(-1).body.fast, null);
  assert(elements["model-main"].innerHTML.includes('value="group:team"'));
  assert(elements["model-fast"].innerHTML.includes("&lt;编码组>"));
  elements["model-group"].value = "team";
  fillModelGroup();
  elements["model-group-add"].value = "alpha";
  addModelGroupMember();
  moveModelGroupMember(1, -1);
  assert(elements["model-group-members"].innerHTML.indexOf("&lt;private>") < elements["model-group-members"].innerHTML.indexOf("二号"));
  reject = true;
  await saveModelGroup();
  assert.deepEqual(calls.at(-1), {method:"POST", path:"/api/models/groups",
    body:{expected_revision:7, group:{id:"team", name:"<编码组>", profile_ids:["alpha", "beta"]}}});
  assert.deepEqual(modelGroupMembers, ["alpha", "beta"]);
  reject = false;
  await reloadModels();
  assert.equal(calls.at(-1).path, "/api/models/reload");
  reject = true;
  elements["model-key"].value = "failed-secret";
  await saveModelProfile();
  assert.equal(elements["model-key"].value, "");
  assert(elements["model-message"].textContent.includes("model_revision_conflict"));
  assert.equal(modelBusy, false);
  closeModels();
})().catch(error => { console.error(error); process.exit(1); });
'''
    result = subprocess.run([node, "-e", harness + script + assertions], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
