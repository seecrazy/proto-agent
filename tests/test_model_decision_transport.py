import json
import threading
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from proto.exec.agentic import LOOP_SYSTEM
from proto.llm.breaker import describe_model_failure
from proto.llm.client import AnthropicCompatClient, LLMError, OpenAICompatClient
from proto.llm.decision import DECISION_NAME, parse_decision
from proto.llm.errors import ModelRequestStopped, ModelResponseError
from proto.fleet import runs as dispatcher
from proto.fleet.activity import activity_reply
from proto.fleet.manager import AgentManager
from proto.system.execution_log import execution_logging
from tests.test_m66_fleet_runs import console, request
from tests.test_m71_continuity import DONE, PASS, WRITE, Model, member, record_writes, start_autonomous, step


def sse(value):
    message = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return f"data: {message}\r\n\r\n".encode()


def openai_delta(delta, finish=None):
    return sse({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]})


def openai_tool(arguments, *, index=0, name=None, identifier=None):
    function = {"arguments": arguments}
    if name is not None:
        function["name"] = name
    call = {"index": index, "function": function}
    if identifier:
        call["id"] = identifier
    return openai_delta({"tool_calls": [call]})


def openai_decision(decision):
    arguments = json.dumps(decision, ensure_ascii=False)
    middle = len(arguments) // 2
    return [openai_delta({"reasoning_content": "private-reasoning", "content": "准备执行。"}),
            openai_tool(arguments[:middle], name=DECISION_NAME, identifier="first"),
            openai_tool(arguments[middle:]), openai_delta({}, "tool_calls"),
            sse({"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 11}}), sse("[DONE]")]


def anthropic_decision(decision):
    arguments = json.dumps(decision, ensure_ascii=False)
    return [sse({"type": "message_start", "message": {"usage": {"input_tokens": 20, "output_tokens": 1}}}),
            sse({"type": "content_block_start", "index": 1, "content_block": {
                "type": "tool_use", "id": "first", "name": DECISION_NAME, "input": {}}}),
            *[sse({"type": "content_block_delta", "index": 1, "delta": {
                "type": "input_json_delta", "partial_json": fragment}})
              for fragment in (arguments[:8], arguments[8:])],
            sse({"type": "content_block_stop", "index": 1}),
            sse({"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 11}}),
            sse({"type": "message_stop"})]


def plain(text):
    return ("application/json", [json.dumps({"choices": [{"message": {"content": text},
                                                         "finish_reason": "stop"}]}).encode()])


@pytest.fixture
def wire():
    fixture = SimpleNamespace(replies=deque(), requests=[])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            fixture.requests.append((self.path, body))
            if not fixture.replies:
                self.send_error(500)
                return
            reply = fixture.replies.popleft()
            content_type, chunks = reply if isinstance(reply, tuple) else ("text/event-stream", reply)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.end_headers()
            try:
                for chunk in chunks:
                    if callable(chunk):
                        chunk()
                    else:
                        self.wfile.write(chunk)
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()
        fixture.url = f"http://127.0.0.1:{server.server_port}"
        try:
            yield fixture
        finally:
            server.shutdown()
            worker.join(timeout=2)


@pytest.mark.parametrize("client_type,frames,path", [
    (OpenAICompatClient, openai_decision, "/chat/completions"),
    (AnthropicCompatClient, anthropic_decision, "/v1/messages"),
])
def test_native_stream_assembles_one_decision_and_reports_usage(wire, client_type, frames, path):
    wire.replies.append(frames(WRITE))
    first = []
    client = client_type(wire.url, "test-key", execution_mode="native")
    raw = client.complete_decision(LOOP_SYSTEM, "task", first_event=first.append, cancelled=lambda: "")
    assert parse_decision(raw) == WRITE
    assert client.last_usage == {"input_tokens": 20, "output_tokens": 11}
    assert len(first) == 1 and first[0] >= 0
    outbound_path, request = wire.requests[0]
    assert outbound_path == path and request["stream"] and 0 < request["max_tokens"] <= 8192
    tool = request["tools"][0].get("function", request["tools"][0])
    assert tool["name"] == DECISION_NAME and len(request["tools"]) == 1
    expected_choice = ({"type": "function", "function": {"name": DECISION_NAME}}
                       if client_type is OpenAICompatClient else {"type": "tool", "name": DECISION_NAME})
    assert request["tool_choice"] == expected_choice and "thinking" not in request
    assert "private-reasoning" not in raw and "准备执行" not in raw


@pytest.mark.parametrize("response,reason", [
    (openai_decision(WRITE)[:-1], "model_stream_interrupted"),
    ([openai_tool('{"tool":', name=DECISION_NAME), openai_delta({}, "tool_calls"), sse("[DONE]")],
     "model_invalid_response"),
    # 多交的那个决策（index=1）被丢掉；第一个的参数 {} 本身不是合法决策，所以仍然无效
    ([openai_tool('{}', name=DECISION_NAME), openai_tool('{}', index=1), openai_delta({}, "tool_calls"),
      sse("[DONE]")], "model_invalid_response"),
    ([openai_delta({}, "length"), sse("[DONE]")], "model_output_truncated"),
    ([sse({"error": {"type": "rate_limit_error", "message": "private"}})], "model_rate_limited"),
    ([sse({"error": {"type": "invalid_request_error", "code": "context_length_exceeded"}})], "model_context_limit"),
])
def test_incomplete_or_rejected_stream_never_yields_action(wire, response, reason):
    wire.replies.append(response)
    client = OpenAICompatClient(wire.url, execution_mode="native")
    with pytest.raises(ModelResponseError, match=reason) as error:
        client.complete_decision(LOOP_SYSTEM, "task", first_event=lambda _: None, cancelled=lambda: "")
    assert describe_model_failure(error.value).reason == reason and client.last_usage is None
    assert "private" not in str(error.value)


@pytest.mark.parametrize("mutate", [lambda frames: frames[1:], lambda frames: frames[:4] + frames[5:],
                                   lambda frames: frames[:-2] + frames[-1:]])
def test_anthropic_stream_requires_started_closed_and_finished_message(wire, mutate):
    wire.replies.append(mutate(anthropic_decision(WRITE)))
    client = AnthropicCompatClient(wire.url, "test-key", execution_mode="native")
    with pytest.raises(ModelResponseError, match="model_invalid_response|model_stream_interrupted"):
        client.complete_decision(LOOP_SYSTEM, "task", first_event=lambda _: None, cancelled=lambda: "")


@pytest.mark.parametrize("finish", ["stop", "length"])
def test_json_fallback_preserves_text_compatibility_and_rejects_truncation(wire, finish):
    wire.replies.append(("application/json", [json.dumps({
        "choices": [{"message": {"content": "已核对。\n" + json.dumps(DONE)}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 20}}).encode()]))
    client = OpenAICompatClient(wire.url, execution_mode="native")
    if finish == "length":
        with pytest.raises(ModelResponseError, match="model_output_truncated"):
            client.complete_decision(LOOP_SYSTEM, "task", first_event=lambda _: None, cancelled=lambda: "")
    else:
        assert parse_decision(client.complete_decision(
            LOOP_SYSTEM, "task", first_event=lambda _: None, cancelled=lambda: "")) == DONE
    assert client.last_usage is None


def test_bigmodel_uses_its_own_tool_stream_option_and_last_usage(wire, monkeypatch):
    relay = OpenAICompatClient(wire.url, execution_mode="native")
    client = OpenAICompatClient("https://open.bigmodel.cn/api/paas/v4", model="glm-5.3")
    monkeypatch.setattr(client, "_post", relay._post)
    chunks = openai_decision(DONE)
    chunks[-3:-1] = [sse({"choices": [{"index": 0, "delta": {"role": "assistant", "content": ""},
                                     "finish_reason": "tool_calls"}],
                         "usage": {"prompt_tokens": 20, "completion_tokens": 11}})]
    chunks.insert(0, sse({"choices": [{"index": 0, "delta": {"reasoning_content": "private"}}],
                         "usage": {"prompt_tokens": 20, "completion_tokens": 1}}))
    wire.replies.append(chunks)
    assert parse_decision(client.complete_decision(
        LOOP_SYSTEM, "task", first_event=lambda _: None, cancelled=lambda: "")) == DONE
    assert client.last_usage == {"input_tokens": 20, "output_tokens": 11}
    request = wire.requests[0][1]
    assert request["tool_stream"] is True and "stream_options" not in request
    assert request["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("received", [False, True])
def test_timeout_distinguishes_waiting_for_first_event_from_stream_gap(wire, received):
    release = threading.Event()
    wire.replies.append([*([openai_delta({"role": "assistant"})] if received else []), lambda: release.wait(2)])
    client = OpenAICompatClient(wire.url, timeout_s=0.22, execution_mode="native")
    first = []
    try:
        with pytest.raises(ModelResponseError, match="model_timeout") as error:
            client.complete_decision(LOOP_SYSTEM, "task", first_event=first.append, cancelled=lambda: "")
        assert error.value.stage == ("stream" if received else "first_event")
        assert bool(first) is received
    finally:
        release.set()


def test_heartbeats_do_not_keep_an_idle_stream_alive(wire):
    stopped = threading.Event()
    finished_heartbeats = threading.Event()

    def pause():
        stopped.wait(0.03)

    initial = anthropic_decision(WRITE)[:1]
    heartbeats = [frame for _ in range(30) for frame in (sse({"type": "ping"}), pause)]
    wire.replies.append(initial + heartbeats + [finished_heartbeats.set])
    client = AnthropicCompatClient(wire.url, "test-key", timeout_s=0.25, execution_mode="native")
    try:
        with pytest.raises(ModelResponseError, match="model_timeout") as error:
            client.complete_decision(LOOP_SYSTEM, "task", first_event=lambda _: None, cancelled=lambda: "")
        assert error.value.stage == "stream" and not finished_heartbeats.is_set()
    finally:
        stopped.set()


def test_cancellation_discards_stream_even_if_action_was_received(wire):
    wire.replies.append(openai_decision(WRITE))
    first = []
    client = OpenAICompatClient(wire.url, execution_mode="native")
    with pytest.raises(ModelRequestStopped, match="run_cancelled"):
        client.complete_decision(LOOP_SYSTEM, "task", first_event=first.append,
                                 cancelled=lambda: "run_cancelled" if first else "")


def test_live_endpoint_reports_streaming_before_any_tool_is_dispatched(wire, tmp_path, monkeypatch):
    client = OpenAICompatClient(wire.url, execution_mode="native")
    fleet, agent = member(tmp_path, client)
    seen = threading.Event()
    release = threading.Event()
    emit = agent.activity_event

    def activity(phase, **fields):
        emit(phase, **fields)
        if phase == "model_streaming":
            seen.set()

    monkeypatch.setattr(agent, "activity_event", activity)
    frames = openai_decision(WRITE)
    wire.replies.extend([frames[:-1] + [lambda: release.wait(5), frames[-1]],
                         openai_decision(DONE), plain(json.dumps(PASS))])
    effects = tmp_path / "effects.txt"
    record_writes(agent, effects)
    with console(fleet, workers=1) as server:
        agent.llm_fast = None
        run, _ = start_autonomous(fleet, agent)
        try:
            assert seen.wait(3)
            status, feed = request(server, "/api/activity/live")
            assert status == 200 and feed["current"]["phase"] == "model_streaming"
            assert feed["current"]["run_id"] == run["id"] and not effects.exists()
            status, reply = request(server, "/api/chat", {"text": "你在干嘛？"})
            assert status == 200 and reply["read_only"] and "等完整决策" in reply["reply"]
            assert len(wire.requests) == 1
        finally:
            release.set()
        assert fleet.runs.wait(run["id"], timeout=5)["status"] == "succeeded"
        assert effects.read_text() == "写入\n"


def test_stream_size_bound_includes_unproductive_frames(wire, monkeypatch):
    monkeypatch.setattr("proto.llm.decision_stream._MAX_RESPONSE", 64)
    wire.replies.append([b": " + b"x" * 100 + b"\n\n"])
    client = OpenAICompatClient(wire.url, execution_mode="native")
    with pytest.raises(ModelResponseError, match="model_response_too_large"):
        client.complete_decision(LOOP_SYSTEM, "task", first_event=lambda _: None, cancelled=lambda: "")


def test_legacy_prose_prefix_and_fences_preserve_exact_arguments():
    decision = dict(WRITE, args={"path": "Product.md", "content": "```json\n{}\n```"})
    raw = json.dumps(decision, ensure_ascii=False)
    assert parse_decision("我先看项目内容。\n" + raw) == decision
    assert parse_decision(f"```JSON\n{raw}\n```") == decision
    for ambiguous in (raw + raw, "{不是动作} " + raw, raw + "此动作已取消", '{"tool":"a","tool":"b"}'):
        with pytest.raises(ValueError):
            parse_decision(ambiguous)


@pytest.mark.parametrize("first", [True, False])
def test_interrupted_stream_resumes_same_run_without_repeating_tools(wire, tmp_path, monkeypatch, first):
    monkeypatch.setattr(dispatcher, "_MODEL_RETRY_DELAYS", (0.0, 0.0, 0.0))
    partial = openai_decision(WRITE)[:-1]
    if first:
        wire.replies.extend([partial, openai_decision(WRITE)])
    else:
        wire.replies.extend([openai_decision(WRITE), partial])
    wire.replies.extend([openai_decision(DONE), plain(json.dumps(PASS))])
    client = OpenAICompatClient(wire.url, execution_mode="native")
    fleet, agent = member(tmp_path, client)
    before, _ = start_autonomous(fleet, agent)
    side_effects = tmp_path / "effects.txt"
    record_writes(agent, side_effects)
    parked = step(fleet)
    assert parked["id"] == before["id"] and parked["status"] == "queued"
    assert parked["result"]["model_reason"] == "model_stream_interrupted"
    assert side_effects.exists() is (not first)
    fleet.runs.stop()
    restarted = AgentManager(tmp_path, llm=client)
    record_writes(restarted.get(agent.fleet_id), side_effects)
    resumed = step(restarted)
    assert resumed["id"] == before["id"] and resumed["session_id"] == parked["session_id"]
    assert resumed["status"] == "succeeded" and side_effects.read_text() == "写入\n"
    assert "stream" not in wire.requests[-1][1] and "tools" not in wire.requests[-1][1]


def test_group_stream_fallback_is_bounded_and_diagnostics_are_private(wire, tmp_path, monkeypatch):
    wire.replies.extend([openai_decision(WRITE)[:-1], openai_decision(DONE)])
    fleet, agent = member(tmp_path, OpenAICompatClient(wire.url, "primary-secret", execution_mode="native"))
    catalog = fleet.enable_model_updates()
    snapshot = catalog.view()
    first = snapshot["selection"]["main"]
    backup = catalog.save_profile({"name": "backup", "base_url": wire.url, "api_key": "backup-secret",
                                   "api_style": "openai", "model": "fixture-backup", "execution_mode": "native"},
                                  snapshot["revision"])
    grouped = catalog.save_group({"name": "group", "profile_ids": [first, backup["saved_id"]]}, backup["revision"])
    catalog.select("group:" + grouped["saved_group_id"], None, grouped["revision"])
    admission = fleet.runs.model_pressure.admit
    priorities = []

    @contextmanager
    def trace_admission(route, *, origin, foreground):
        priorities.append(foreground)
        with admission(route, origin=origin, foreground=foreground):
            yield

    monkeypatch.setattr(fleet.runs.model_pressure, "admit", trace_admission)
    with execution_logging(tmp_path, "test"):
        assert parse_decision(agent._complete(
            agent.llm, LOOP_SYSTEM, "private-prompt", decision=True, foreground=True)) == DONE
    assert priorities == [True, True]
    log = (tmp_path / ".runtime-logs/execution.jsonl").read_text()
    entries = [json.loads(line) for line in log.splitlines()]
    failures = [entry for entry in entries if entry["event"] == "model_failed"]
    returned = [entry for entry in entries if entry["event"] == "model_returned"]
    assert len(failures) == len(returned) == 1 and len(wire.requests) == 2
    assert failures[0]["stage"] == "stream" and failures[0]["reason"] == "model_stream_interrupted"
    assert returned[0]["tokens"] == 31 and returned[0]["attempt"] == 2
    assert failures[0]["call_id"] == returned[0]["call_id"]
    assert not any(secret in log for secret in ("primary-secret", "backup-secret", "private-prompt", "private-reasoning", "Product.swift"))


def test_unknown_model_error_gets_one_safe_retry_and_separate_current_status(tmp_path, monkeypatch):
    monkeypatch.setattr(dispatcher, "_MODEL_RETRY_DELAYS", (0.0, 0.0, 0.0))
    failure = LLMError("private fixture without a structured error")
    fleet, agent = member(tmp_path, Model([TimeoutError(), WRITE, failure, failure]))
    start_autonomous(fleet, agent)
    assert step(fleet)["status"] == "failed"
    original, _ = start_autonomous(fleet, agent)
    side_effects = tmp_path / "effects.txt"
    record_writes(agent, side_effects)
    assert step(fleet)["result"]["model_retries"] == 1
    parked = step(fleet)
    assert parked["id"] == original["id"] and parked["reason"] == "model_retry_exhausted"
    assert parked["result"]["model_reason"] == "model_request_failed"
    assert side_effects.read_text() == "写入\n"
    snapshot = fleet.runs.activity.snapshot(agent)
    current = activity_reply(snapshot)
    historical = activity_reply(snapshot, detailed=True)
    assert "这轮卡在：模型请求失败" in current and "模型响应超时" not in current
    assert "此前轮次的原因" in historical and "模型响应超时" in historical


def test_native_capability_detection_is_conservative_and_can_be_disabled():
    assert OpenAICompatClient("https://open.bigmodel.cn/api/paas/v4", model="glm-5.3").native_decisions
    assert AnthropicCompatClient("https://open.bigmodel.cn/api/anthropic", "fixture").native_decisions
    assert not OpenAICompatClient("https://open.bigmodel.cn.example/api/paas/v4", model="glm-5.3").native_decisions
    assert not OpenAICompatClient("https://open.bigmodel.cn/api/paas/v4", model="other").native_decisions
    assert not OpenAICompatClient("https://open.bigmodel.cn/api/paas/v4", model="glm-5.3",
                                  execution_mode="legacy").native_decisions
