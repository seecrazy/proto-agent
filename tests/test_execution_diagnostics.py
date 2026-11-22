import io
import json
import socket
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError

import pytest

from proto import run
from proto.llm.breaker import describe_model_failure, model_wait_reason
from proto.llm.client import AnthropicCompatClient, LLMError, ModelPressureWait, OpenAICompatClient
from proto.runtime.model_pressure import retry_after
from proto.system import execution_log
from proto.system.execution_log import execution_logging, record_execution
from proto.web import __main__ as web
from tests.test_m64_live_models import profile
from tests.test_m76_model_groups import complete, configured


LOG_NAME = ".runtime-logs/execution.jsonl"


def rows(root):
    return [json.loads(line) for line in (root / LOG_NAME).read_text(encoding="utf-8").splitlines()]


def test_wrapped_socket_timeout_has_stable_reason():
    timeout = socket.timeout("private timeout")
    transport = URLError(timeout)
    transport.__cause__ = OSError("a connection wrapper")
    failure = LLMError("private wrapper")
    failure.__cause__ = transport
    assert model_wait_reason(failure) == "model_timeout"
    details = describe_model_failure(failure)
    assert details.exception_type == type(timeout).__name__ and details.http_status is None
    assert model_wait_reason(URLError("backend unavailable")) == "model_unreachable"


@pytest.mark.parametrize("style,client_type", [("openai", OpenAICompatClient), ("anthropic", AnthropicCompatClient)])
def test_real_read_timeout_uses_backup_and_logs_actual_attempts(tmp_path, monkeypatch, style, client_type):
    received = []
    release = threading.Event()

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(payload["model"])
            if payload["model"] == "primary":
                release.wait(5)
                return
            reply = b'{"choices":[{"message":{"content":"private model reply"}}]}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, *_args):
            pass

    original_init = client_type.__init__

    def short_timeout(client, **kwargs):
        if kwargs["model"] == "primary":
            kwargs["timeout_s"] = 0.25
        original_init(client, **kwargs)

    monkeypatch.setattr(client_type, "__init__", short_timeout)
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}/proxy"
        try:
            fleet, agent, _ = configured(tmp_path, profile("primary", base, style=style), profile("fallback", base))
            with execution_logging(tmp_path, "web"):
                assert agent._complete(agent.llm_fast, "private system prompt", "private user prompt") == "private model reply"
            assert fleet.runs.budget.view()["used"]["calls"] == 2
            assert fleet.runs.budget.view()["uncertain"] == 1
        finally:
            release.set()
            server.shutdown()
            thread.join(timeout=4)
    assert received == ["primary", "fallback"]
    log = rows(tmp_path)
    model = [row for row in log if row["event"].startswith("model_")]
    assert [(row["event"], row["profile_id"], row["attempt"]) for row in model] == [
        ("model_started", "primary", 1), ("model_failed", "primary", 1),
        ("model_started", "fallback", 2), ("model_returned", "fallback", 2)]
    assert model[1]["reason"] == "model_timeout" and model[1]["duration_ms"] >= 200
    assert model[1]["failover_eligible"] and model[1]["exception_type"] == socket.timeout.__name__
    assert model[-1]["tokens"] > 0 and model[-1]["role"] == "fast"
    assert len({row["call_id"] for row in model}) == 1
    assert all(row["agent_id"] == agent.fleet_id and "run_id" not in row for row in model)
    contents = (tmp_path / LOG_NAME).read_text(encoding="utf-8")
    for private in ("private system prompt", "private user prompt", "test-private-key", "private model reply", base):
        assert private not in contents


@pytest.mark.parametrize("client_type", [OpenAICompatClient, AnthropicCompatClient])
def test_http_error_uses_status_and_retry_header_without_reading_error_body(tmp_path, monkeypatch, client_type):
    class UnreadableError(io.BytesIO):
        def read(self, *_args):
            raise AssertionError("must not read an error body")

    body = UnreadableError(b"private error body")

    def rate_limited(*_args, **_kwargs):
        raise HTTPError("https://private.invalid/v1", 429, "private HTTP reason", {"Retry-After": "12"}, body)

    monkeypatch.setattr("urllib.request.urlopen", rate_limited)
    style = "openai" if client_type is OpenAICompatClient else "anthropic"
    fleet, agent, _ = configured(tmp_path, profile("limited", "https://private.invalid/v1", style=style))
    fleet.runs.model_pressure.now = lambda: 100
    with execution_logging(tmp_path, "web"):
        with pytest.raises(ModelPressureWait) as blocked:
            complete(agent)
        assert blocked.value.retry_at == 112
        assert body.closed
    failure = next(row for row in rows(tmp_path) if row["event"] == "model_failed")
    assert (failure["http_status"], failure["reason"], failure["retry_at"]) == (429, "model_rate_limited", 112)
    assert "private" not in (tmp_path / LOG_NAME).read_text(encoding="utf-8")

    body = UnreadableError(b"private error body")
    with pytest.raises(LLMError, match="HTTP 429") as error:
        client_type("https://private.invalid/v1", "private-key").complete("system", "user")
    assert body.closed and retry_after(error.value, 100) == 112
    assert "private" not in str(error.value)


def test_local_busy_budget_and_tool_phases_do_not_log_fake_model_attempts(tmp_path):
    fleet, agent, _ = configured(tmp_path, "primary")
    client = agent.llm.plan().members[0]
    pressure = fleet.runs.model_pressure
    with execution_logging(tmp_path, "web"):
        with pressure.admit(client.pressure_key, origin=client.pressure_origin):
            with pytest.raises(ModelPressureWait, match="model_route_busy"):
                complete(agent)
        fleet.runs.budget.configure(0, 0)
        with pytest.raises(LLMError, match="fleet_budget_exhausted"):
            complete(agent)
        with pytest.raises(RuntimeError, match="private tool argument"):
            with agent._tool_io("run_command"):
                raise RuntimeError("private tool argument")
    log = rows(tmp_path)
    assert not any(row["event"] == "model_started" for row in log)
    assert fleet.runs.budget.view()["used"]["calls"] == 0
    blocked = [row for row in log if row["event"] == "model_call_failed"]
    assert [row["reason"] for row in blocked] == ["model_route_busy", "fleet_budget_exhausted"]
    skipped = next(row for row in log if row["event"] == "model_candidate_skipped")
    assert skipped["profile_id"] == "primary" and skipped["call_id"] == blocked[0]["call_id"]
    tool = next(row for row in log if row["event"] == "tool_failed")
    assert tool["tool"] == "run_command" and tool["exception_type"] == "RuntimeError" and tool["duration_ms"] >= 0
    assert "private tool argument" not in (tmp_path / LOG_NAME).read_text(encoding="utf-8")


def test_recovery_is_logged_as_the_committed_run_state(tmp_path):
    fleet, agent, _ = configured(tmp_path, "primary")
    run_info = fleet.runs.enqueue(agent.fleet_id, "task", {"text": "private task argument"})
    claimed, _, lock = fleet.runs._pick()
    lock.release()
    assert claimed["id"] == run_info["id"]
    with execution_logging(tmp_path, "web"):
        fleet.runs.recover()
    current = fleet.runs.get(claimed["id"])
    event = next(row for row in rows(tmp_path) if row["event"] == "run_recovered")
    assert (event["run_id"], event["status"], event["reason"]) == (current["id"], current["status"], current["reason"])
    assert current["status"] == "needs_reconciliation"
    assert "private task argument" not in (tmp_path / LOG_NAME).read_text(encoding="utf-8")


@pytest.mark.parametrize("source", ["cli", "web"])
def test_entrypoints_start_and_close_private_execution_log(tmp_path, monkeypatch, source):
    monkeypatch.chdir(tmp_path)
    if source == "cli":
        monkeypatch.delenv("PROTO_LLM_BASE_URL", raising=False)
        monkeypatch.setattr(run, "create_llm_stack", lambda: None)
        monkeypatch.setattr("builtins.input", lambda _prompt: "/quit")
        assert run.main(["--data-dir", str(tmp_path)]) == 0
    else:
        monkeypatch.delenv("PROTO_CAFFEINATE", raising=False)
        monkeypatch.setattr(web, "create_llm_stack", lambda: None)
        monkeypatch.setattr(web.ProtoWebServer, "serve_forever", lambda _server: None)
        monkeypatch.setattr(sys, "argv", ["proto.web", "--data-dir", str(tmp_path), "--port", "0", "--no-open",
                                          "--token", "private-console-token"])
        assert web.main() == 0
    records = rows(tmp_path)
    assert [row["event"] for row in records if row["event"].startswith("runtime_")] == [
        "runtime_started", "runtime_ready", "runtime_stopped"]
    assert {row["source"] for row in records} == {source}
    assert len({row["boot_id"] for row in records}) == 1
    assert "private-console-token" not in (tmp_path / LOG_NAME).read_text(encoding="utf-8")
    assert stat.S_IMODE((tmp_path / LOG_NAME).stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / LOG_NAME).parent.stat().st_mode) == 0o700


def test_log_rotates_and_filters_unknown_fields_and_reasons(tmp_path, monkeypatch):
    monkeypatch.setattr(execution_log, "_MAX_BYTES", 520)
    with execution_logging(tmp_path, "cli"):
        for generation in range(30):
            record_execution("run_finished", generation=generation, reason="very_private_token",
                             response="raw provider data", status="failed")
    files = sorted((tmp_path / LOG_NAME).parent.glob("execution.jsonl*"))
    assert [path.name for path in files] == ["execution.jsonl", "execution.jsonl.1", "execution.jsonl.2", "execution.jsonl.3"]
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 and path.stat().st_size <= 520 for path in files)
    retained = [json.loads(line) for path in files for line in path.read_text(encoding="utf-8").splitlines()]
    generations = [row["generation"] for row in retained if row["event"] == "run_finished"]
    assert max(generations) == 29 and min(generations) > 0
    assert all(row.get("reason", "other") == "other" and "response" not in row for row in retained)


@pytest.mark.parametrize("linked_path", ["directory", "file"])
def test_log_does_not_follow_links(tmp_path, capsys, linked_path):
    target = tmp_path / "unrelated"
    log = tmp_path / LOG_NAME
    if linked_path == "directory":
        target.mkdir()
        log.parent.symlink_to(target, target_is_directory=True)
    else:
        target.write_text("leave it alone", encoding="utf-8")
        log.parent.mkdir()
        log.symlink_to(target)
    with execution_logging(tmp_path, "cli"):
        record_execution("runtime_ready")
    assert capsys.readouterr().err.count("执行日志不可用") == 1
    if linked_path == "directory":
        assert not list(target.iterdir())
    else:
        assert target.read_text(encoding="utf-8") == "leave it alone"


def test_log_io_failure_does_not_stop_model_execution(tmp_path, monkeypatch, capsys):
    fleet, agent, _ = configured(tmp_path, "primary")

    def fail_write(*_args):
        raise OSError("private filesystem detail")

    monkeypatch.setattr(execution_log._PrivateRotatingHandler, "shouldRollover", fail_write)
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs:
                        io.BytesIO(b'{"choices":[{"message":{"content":"ok"}}]}'))
    with execution_logging(tmp_path, "web"):
        assert complete(agent) == "ok"
        assert complete(agent) == "ok"
    stderr = capsys.readouterr().err
    assert stderr.count("执行日志不可用") == 1 and "private filesystem detail" not in stderr
    assert fleet.runs.budget.view()["used"]["calls"] == 2
