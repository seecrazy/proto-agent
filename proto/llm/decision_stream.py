"""Receive complete OpenAI or Anthropic decisions without executing streamed input."""
from __future__ import annotations

import json
import time
from http.client import HTTPException

from proto.llm.decision import DECISION_NAME, decision_json, parse_decision
from proto.llm.errors import NETWORK_TIMEOUTS, ModelRequestStopped, ModelResponseError


_MAX_RESPONSE = 4 * 1024 * 1024
_MAX_FRAME = 512 * 1024
_MAX_STREAM_SECONDS = 240
_CANCEL_CHECK_SECONDS = 0.25
_PROVIDER_ERRORS = {
    "authentication_error": ("model_unauthorized", 401), "permission_error": ("model_forbidden", 403),
    "rate_limit_error": ("model_rate_limited", 429), "rate_limit_exceeded": ("model_rate_limited", 429),
    "overloaded_error": ("model_server_error", 529), "server_error": ("model_server_error", 500),
    "api_error": ("model_server_error", 500), "invalid_request_error": ("model_http_error", 400),
    "context_length_exceeded": ("model_context_limit", 400),
}


def _mapping(value):
    if not isinstance(value, dict):
        raise ValueError("expected_object")
    return value


def _provider_error(payload):
    error = _mapping(payload)
    for value in (error.get("code"), error.get("type")):
        if isinstance(value, str) and value in _PROVIDER_ERRORS:
            reason, status = _PROVIDER_ERRORS[value]
            raise ModelResponseError(reason, "stream", status)
    raise ModelResponseError("model_provider_error", "stream")


class _Decision:
    def __init__(self, style):
        self.style = style
        self.text = []
        self.arguments = []
        self.name = ""
        self.call_index = None
        self.call_id = None
        self.input = None
        self.message_started = False
        self.block_closed = False
        self.finish = None
        self.usage = {}
        self.cache_read = 0
        # 模型一步交了多个决策时，第一个照常执行，其余只记数（见 extra_calls）
        self._extras = set()

    @property
    def extra_calls(self):
        return len(self._extras)

    def _usage(self, usage):
        if not isinstance(usage, dict):
            return
        names = ("prompt_tokens", "completion_tokens") if self.style == "openai" else ("input_tokens", "output_tokens")
        for key, source in zip(("input_tokens", "output_tokens"), names):
            value = usage.get(source)
            if type(value) is int and 0 <= value <= 10**11:
                self.usage[key] = value
        cached = usage.get("cache_read_input_tokens")
        if type(cached) is not int and isinstance(usage.get("prompt_tokens_details"), dict):
            cached = usage["prompt_tokens_details"].get("cached_tokens")
        if type(cached) is int and 0 <= cached <= 10**11:
            self.cache_read = cached          # 命中供应方前缀缓存的输入 token（诊断用，不计费口径不变）

    def _text(self, value):
        if value is not None:
            if not isinstance(value, str):
                raise ValueError("invalid_text_delta")
            self.text.append(value)

    def _finish(self, value):
        if value is not None:
            if not isinstance(value, str) or (self.finish is not None and value != self.finish):
                raise ValueError("conflicting_finish")
            self.finish = value

    def _openai(self, event, *, complete=False):
        self._usage(event.get("usage"))
        choices = event.get("choices")
        if not isinstance(choices, list) or len(choices) > 1:
            raise ValueError("invalid_choices")
        progress = False
        for choice in choices:
            item = _mapping(choice)
            if item.get("index", 0) != 0:
                raise ValueError("invalid_choice_index")
            part = item.get("message" if complete else "delta")
            delta = {} if part is None else _mapping(part)
            if self.finish is not None and delta:
                raise ValueError("output_after_finish")
            if delta.get("refusal"):
                raise ModelResponseError("model_response_refused", "decision")
            self._text(delta.get("content"))
            calls = delta.get("tool_calls")
            if calls is not None:
                if not isinstance(calls, list):
                    raise ValueError("invalid_tool_calls")
                for position, call in enumerate(calls):
                    span = _mapping(call)
                    # 非流式回包里的 tool_calls 不带 index，按列表位置区分
                    index = span.get("index", position if complete else 0)
                    if index != 0 or span.get("type", "function") != "function":
                        self._extras.add(("call", str(index)))
                        continue
                    identity = span.get("id")
                    if identity is not None and (not isinstance(identity, str) or (
                            self.call_id is not None and self.call_id != identity)):
                        raise ValueError("conflicting_tool_call")
                    self.call_id = identity if identity is not None else self.call_id
                    self.call_index = 0
                    function = _mapping(span.get("function", {}))
                    name = function.get("name")
                    args = function.get("arguments")
                    if name is not None:
                        if not isinstance(name, str):
                            raise ValueError("invalid_function_name")
                        self.name += name
                    if args is not None:
                        if not isinstance(args, str):
                            raise ValueError("invalid_argument_delta")
                        self.arguments.append(args)
            finish = item.get("finish_reason")
            self._finish(finish)
            progress = bool(delta) or finish is not None
        return progress

    def _anthropic_block(self, block, index, *, complete=False):
        item = _mapping(block)
        if item.get("type") == "tool_use":
            if self.call_index is None:
                if type(index) is not int or index < 0:
                    raise ValueError("invalid_tool_call")
                self.call_index = index
                self.name = item.get("name")
                self.input = _mapping(item.get("input", {}))
                self.block_closed = complete
            elif self.block_closed and index != self.call_index:
                # 第一个工具块已经闭合，又开了一个：模型一步交了第二个决策。
                # 此前整条回复判无效——连前面已完整生成的大文件写入也一起丢，
                # 白等几十秒再重试（生产上 model_invalid_response 全是这个）。
                self._extras.add(("block", index))
            else:
                raise ValueError("multiple_tool_calls")
        elif item.get("type") == "text":
            self._text(item.get("text"))

    def _anthropic(self, event):
        kind = event.get("type")
        index = event.get("index")
        if kind == "message_start":
            if self.message_started:
                raise ValueError("duplicate_message_start")
            self.message_started = True
            self._usage(_mapping(event.get("message")).get("usage"))
            return True
        if kind not in ("content_block_start", "content_block_delta", "content_block_stop", "message_delta", "message_stop"):
            return False
        if not self.message_started:
            raise ValueError("missing_message_start")
        if kind == "message_stop":
            return True
        if kind == "message_delta":
            if self.call_index is not None and not self.block_closed:
                raise ValueError("unclosed_tool_block")
            self._finish(_mapping(event.get("delta")).get("stop_reason"))
            self._usage(event.get("usage"))
            return True
        if self.finish is not None:
            raise ValueError("output_after_finish")
        if kind == "content_block_start":
            self._anthropic_block(event.get("content_block"), index)
        elif kind == "content_block_delta":
            delta = _mapping(event.get("delta"))
            if delta.get("type") == "text_delta":
                self._text(delta.get("text"))
            elif delta.get("type") == "input_json_delta":
                args = delta.get("partial_json")
                if not isinstance(args, str):
                    raise ValueError("invalid_argument_delta")
                if ("block", index) in self._extras:
                    return True                      # 多余那个决策的参数增量：收下不看
                if self.call_index is None or index != self.call_index or self.block_closed or self.input:
                    raise ValueError("invalid_argument_delta")
                self.arguments.append(args)
        elif kind == "content_block_stop" and self.call_index is not None and index == self.call_index:
            if self.block_closed:
                raise ValueError("duplicate_tool_block_stop")
            self.block_closed = True
        return True

    def feed(self, payload, *, complete=False):
        event = _mapping(payload)
        if "error" in event or event.get("type") == "error":
            _provider_error(event.get("error"))
        if not complete and event.get("type") == "ping":
            return False
        if self.style == "openai":
            return self._openai(event, complete=complete)
        if not complete:
            return self._anthropic(event)
        self._usage(event.get("usage"))
        self._finish(event.get("stop_reason"))
        if not isinstance(event.get("content"), list):
            raise ValueError("invalid_content")
        for index, block in enumerate(event["content"]):
            self._anthropic_block(block, index, complete=True)
        return True

    def result(self):
        if self.finish in {"length", "max_tokens"}:
            raise ModelResponseError("model_output_truncated", "decision")
        if self.finish in {"content_filter", "refusal", "sensitive"}:
            raise ModelResponseError("model_response_refused", "decision")
        if self.style == "openai":
            tool_finish, text_finishes = "tool_calls", ("stop",)
        else:
            tool_finish, text_finishes = "tool_use", ("end_turn", "stop_sequence")
        if self.finish != tool_finish and self.finish not in text_finishes:
            raise ModelResponseError("model_stream_interrupted", "stream")
        if self.call_index is None:
            if self.finish == tool_finish:
                raise ValueError("missing_tool_call")
            return decision_json(parse_decision("".join(self.text)))
        if self.name != DECISION_NAME or (self.style == "anthropic" and not self.block_closed):
            raise ValueError("unknown_or_incomplete_decision_tool")
        value = parse_decision("".join(self.arguments), strict=True) if self.arguments else self.input
        return decision_json(value)


class _Reader:
    def __init__(self, response, started, timeout, first_event, cancelled):
        self.response = response
        self.started = started
        self.timeout = timeout
        self.first_event = first_event
        self.cancelled = cancelled
        self.last_event_at = None
        self.cancel_check_at = started
        self.total = 0

    @property
    def stage(self):
        return "stream" if self.last_event_at is not None else "first_event"

    def check(self, *, force_cancel=False):
        now = time.monotonic()
        if force_cancel or now >= self.cancel_check_at:
            reason = self.cancelled()
            if reason:
                raise ModelRequestStopped(reason)
            now = time.monotonic()
            self.cancel_check_at = now + _CANCEL_CHECK_SECONDS
        last_event = self.started if self.last_event_at is None else self.last_event_at
        if now - self.started >= _MAX_STREAM_SECONDS or now - last_event >= self.timeout:
            raise ModelResponseError("model_timeout", self.stage)
        return now

    def read(self):
        self.check()
        try:
            chunk = self.response.read1(16 * 1024)
        except NETWORK_TIMEOUTS:
            raise ModelResponseError("model_timeout", self.stage) from None
        except (HTTPException, OSError):
            raise ModelResponseError("model_stream_interrupted", self.stage) from None
        self.check()
        self.total += len(chunk)
        if self.total > _MAX_RESPONSE:
            raise ModelResponseError("model_response_too_large", self.stage)
        return chunk

    def mark_event(self):
        first = self.last_event_at is None
        self.last_event_at = self.check()
        if first:
            self.first_event(round((self.last_event_at - self.started) * 1000))

    def events(self):
        tail = b""
        data = []
        size = 0
        while True:
            chunk = self.read()
            if not chunk:
                return
            *lines, tail = (tail + chunk).split(b"\n")
            if len(tail) > _MAX_FRAME:
                raise ModelResponseError("model_response_too_large", self.stage)
            for line in lines:
                line = line.rstrip(b"\r")
                if not line:
                    if data:
                        yield b"\n".join(data)
                        data, size = [], 0
                elif line.startswith(b"data:"):
                    field = line[5:].removeprefix(b" ")
                    size += len(field) + 1
                    if size > _MAX_FRAME:
                        raise ModelResponseError("model_response_too_large", self.stage)
                    data.append(field)


def read_decision(response, style, *, started, timeout, first_event, cancelled):
    reader = _Reader(response, started, timeout, first_event, cancelled)
    decision = _Decision(style)
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    try:
        if content_type == "application/json":
            parts = []
            while True:
                chunk = reader.read()
                if not chunk:
                    break
                reader.mark_event()
                parts.append(chunk)
            if response.length not in (None, 0):
                raise ModelResponseError("model_stream_interrupted", reader.stage)
            decision.feed(json.loads(b"".join(parts)), complete=True)
        elif content_type == "text/event-stream":
            for data in reader.events():
                if style == "openai" and data.strip() == b"[DONE]":
                    break
                event = _mapping(json.loads(data))
                if decision.feed(event):
                    reader.mark_event()
                if style == "anthropic" and event.get("type") == "message_stop":
                    break
            else:
                raise ModelResponseError("model_stream_interrupted", reader.stage)
        else:
            raise ModelResponseError("model_invalid_response", "headers")
        result = decision.result()
        reader.check(force_cancel=True)
        if decision.extra_calls:
            result = decision_json(dict(json.loads(result), _extra_calls=decision.extra_calls))
        if len(decision.usage) == 2:
            if decision.cache_read:
                decision.usage["cache_read"] = decision.cache_read
            return result, decision.usage
        return result, None
    except (ValueError, TypeError, RecursionError) as exc:
        # 协议哪条假设被打破要能查到：stage 只说了「在哪一步」，detail 说「是什么」
        error = ModelResponseError("model_invalid_response", "decision" if decision.finish else reader.stage)
        error.detail = f"{type(exc).__name__}-{exc}"[:80]
        raise error from None
