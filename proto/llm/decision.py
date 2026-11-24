"""The actor's single decision envelope and provider capability selection."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit


DECISION_NAME = "proto_decision"
DECISION_MODELS = {"glm-5.3", "glm-5.3-flash", "glm-5.3-flashx"}
EXECUTION_MODES = {"auto", "native", "legacy"}
_MAX_DECISION_BYTES = 256 * 1024
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.IGNORECASE | re.DOTALL)


def native_decisions(style, base, model, mode):
    if mode != "auto":
        return mode == "native"
    try:
        endpoint = urlsplit(base)
        paths = {"/api/paas/v4", "/api/coding/paas/v4"} if style == "openai" else {"/api/anthropic"}
        return (endpoint.scheme == "https" and endpoint.hostname == "open.bigmodel.cn"
                and endpoint.port in (None, 443) and endpoint.path in paths
                and not (endpoint.username or endpoint.password or endpoint.query or endpoint.fragment)
                and model.lower() in DECISION_MODELS)
    except ValueError:
        return False


def decision_tool(style):
    schema = {
        "type": "object",
        "properties": {
            "tool": {"type": "string", "description": "Exact tool name from the task's tool catalog."},
            "args": {"type": "object", "description": "Named arguments from that tool's contract."},
            "category": {"type": "string", "enum": ["read_draft", "workspace_write", "visible_write"]},
            "note": {"type": "string", "description": "One short sentence (<=60 chars): why this step advances the task."},
            "done": {"type": "boolean", "description": "True only when reporting completion without a tool."},
            "summary": {"type": "string", "description": "Evidence-based completion summary."},
        },
        "additionalProperties": False,
    }
    function = {"name": DECISION_NAME,
                "description": "Submit one next tool decision (tool, args, category), or finish (done=true, summary)."}
    if style == "anthropic":
        return dict(function, input_schema=schema)
    return {"type": "function", "function": dict(function, parameters=schema)}


def _reject_constant(value):
    raise ValueError("nonfinite_decision_number")


def _unique_object(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate_decision_key")
        result[key] = value
    return result


def validate_decision(decision):
    if not isinstance(decision, dict):
        raise ValueError("决策必须是 JSON 对象")
    if "done" in decision and not isinstance(decision["done"], bool):
        raise ValueError("done 必须是布尔值")
    if decision.get("done") is True:
        if "tool" in decision or "args" in decision:
            raise ValueError("完成决策不能同时调用工具")
        if not isinstance(decision.get("summary", ""), str):
            raise ValueError("summary 必须是字符串")
    else:
        if not isinstance(decision.get("tool"), str) or not decision["tool"].strip():
            raise ValueError("工具决策需要非空 tool")
        if not isinstance(decision.get("args", {}), dict):
            raise ValueError("args 必须是 JSON 对象")
    note = decision.get("note", "")
    if not isinstance(note, str):
        raise ValueError("note 必须是字符串")
    if note:
        decision["note"] = note.strip()[:60]
    return decision


def parse_decision(raw, *, strict=False):
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_DECISION_BYTES:
        raise ValueError("invalid_decision_size")
    candidate = raw.strip()
    if not strict:
        fenced = _FENCE.fullmatch(candidate)
        if fenced:
            candidate = fenced.group(1).strip()
        elif not candidate.startswith("{"):
            offset = candidate.find("{")
            if 0 < offset <= 1024 and not any(mark in candidate[:offset] for mark in ("}", "[", "]", "`")):
                candidate = candidate[offset:]
    try:
        return validate_decision(json.loads(candidate, parse_constant=_reject_constant, object_pairs_hook=_unique_object))
    except RecursionError:
        raise ValueError("decision_nesting_exceeded") from None


def extra_decisions(raw):
    """决策信封里被丢掉的多余决策数（模型一步交了多个时由 read_decision 写入）。"""
    try:
        value = int(json.loads(raw).get("_extra_calls", 0)) if isinstance(raw, str) else 0
    except (ValueError, TypeError, AttributeError):
        return 0
    return max(0, min(value, 50))


def decision_json(decision):
    result = json.dumps(validate_decision(decision), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(result.encode("utf-8")) > _MAX_DECISION_BYTES:
        raise ValueError("invalid_decision_size")
    return result
