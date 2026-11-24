"""Bounded, content-free operational events, separate from the audit spine."""
from __future__ import annotations

import json
import logging
import math
import os
import re
import stat
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4


_LOG = logging.getLogger("proto.execution")
_LOG.setLevel(logging.INFO)
_LOG.propagate = False
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 3
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,80}")
_IDENTIFIERS = frozenset({"agent_id", "run_id", "call_id", "profile_id", "role", "pool",
                          "kind", "status", "tool", "stage", "exception_type", "backend", "rules_version",
                          "detail", "provider_code"})
_NUMBERS = frozenset({"attempt", "duration_ms", "http_status", "retry_at", "generation",
                     "estimated_input_tokens", "tokens", "model_retries", "restart_attempts", "exit_code",
                     "hits_before", "hits_after", "extra_calls", "cache_read_tokens"})
_FLAGS = frozenset({"foreground", "grouped", "failover_eligible", "resumed", "decision", "streamed"})
_REASONS = frozenset({
    "model_stream_interrupted", "model_invalid_response", "model_invalid_decision", "model_output_truncated",
    "model_response_refused", "model_response_too_large", "model_context_limit", "model_provider_error",
    "model_timeout", "model_unreachable", "model_rate_limited", "model_server_error", "model_unauthorized",
    "model_forbidden", "model_not_found", "model_http_error", "model_request_failed", "model_not_configured",
    "model_group_unavailable", "model_route_busy", "model_quota_exhausted", "circuit_breaker_open", "model_retry_wait",
    "model_retry_exhausted", "safe_resume_model", "safe_resume_ready", "safe_resume_limit",
    "interrupted_requeued", "interrupted_without_final_receipt", "unsafe_resume_checkpoint", "plan_interrupted",
    "budget_exhausted", "fleet_budget_exhausted", "budget", "run_cancelled", "agent_unavailable",
    "fleet_paused", "muted", "manual_only", "not_enabled", "suggestions_only", "needs_reconciliation",
    "delivery_no_progress", "delivery_not_verified", "delivery_predecessor_pending", "verification",
    "context_budget_exceeded", "project_directory_unavailable", "ios_delivery_evidence_missing",
    "local_verification_evidence_missing", "execution_disabled", "executable_not_found", "nonzero_exit", "timeout",
    "launch_failed", "command_not_allowed", "xcrun_tool_not_allowed", "cwd_unavailable", "cwd_outside_workspace",
    "shell_syntax_not_supported",
})


def execution_reason(value):
    if not isinstance(value, str) or not value:
        return None
    code = value.removeprefix("llm_error:")
    for budget in ("budget_exhausted", "fleet_budget_exhausted"):
        if code.startswith(budget + ":"):
            return budget
    return code if code in _REASONS else "other"


def record_execution(event, **fields):
    if not _LOG.handlers:
        return
    safe = {}
    for name, value in fields.items():
        if name in ("reason", "model_reason"):
            value = execution_reason(value)
            if value:
                safe[name] = value
        elif name in _IDENTIFIERS and isinstance(value, str) and _IDENTIFIER.fullmatch(value):
            safe[name] = value
        elif name == "detail" and isinstance(value, str) and value:
            # detail 来自我们自己的异常消息（decision_stream 的协议校验），常含中文；
            # 白名单会把它整个滤掉，导致 invalid_response 无从定位（2026-11-24 生产）。
            # 只清洗控制字符与换行，截到 80 字符——不放进通用白名单，避免放开其他字段。
            safe[name] = " ".join(value.split())[:80]
        elif name in _NUMBERS and (type(value) is int or (type(value) is float and math.isfinite(value))):
            safe[name] = value
        elif name in _FLAGS and type(value) is bool:
            safe[name] = value
    _LOG.info(event, extra={"execution_fields": safe})


class _ExecutionFormatter(logging.Formatter):
    def __init__(self, source):
        super().__init__()
        self.source = source
        self.boot_id = uuid4().hex

    def format(self, record):
        return json.dumps({
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "event": record.msg, "source": self.source, "boot_id": self.boot_id,
            "pid": record.process, **record.execution_fields,
        }, separators=(",", ":"), allow_nan=False)


def _warn_unavailable():
    try:
        print("PROTO: 执行日志不可用，本次运行将不再写入执行日志。", file=sys.stderr)
    except (OSError, ValueError):
        pass


class _PrivateRotatingHandler(RotatingFileHandler):
    def __init__(self, path):
        self.unavailable = False
        super().__init__(path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8")

    def _open(self):
        if Path(self.baseFilename).is_symlink():
            raise OSError("invalid_execution_log_file")
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(self.baseFilename, flags, 0o600)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise OSError("invalid_execution_log_file")
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            return os.fdopen(descriptor, "a", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise

    def emit(self, record):
        if not self.unavailable:
            super().emit(record)

    def handleError(self, record):
        if not self.unavailable:
            self.unavailable = True
            _warn_unavailable()


@contextmanager
def execution_logging(data_dir, source):
    handler = None
    try:
        directory = Path(data_dir) / ".runtime-logs"
        directory.mkdir(mode=0o700, exist_ok=True)
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise OSError("invalid_execution_log_directory")
        directory.chmod(0o700)
        handler = _PrivateRotatingHandler(directory / "execution.jsonl")
        handler.setFormatter(_ExecutionFormatter(source))
        _LOG.addHandler(handler)
    except OSError:
        _warn_unavailable()
    try:
        record_execution("runtime_started")
        yield
    except Exception as error:
        record_execution("runtime_failed", exception_type=type(error).__name__)
        raise
    else:
        record_execution("runtime_stopped")
    finally:
        if handler is not None:
            _LOG.removeHandler(handler)
            try:
                handler.close()
            except OSError:
                if not handler.unavailable:
                    _warn_unavailable()
