# -*- coding: utf-8 -*-
"""M19-R3: LLM 熔断器——防重试风暴（生产可靠性模式）。

连续 N 次失败 → 打开（cooldown 秒内不再调用 LLM，直接走离线回退）；
冷却期满 → 半开（放一次试探）；成功 → 关闭。三态机。
"""
from __future__ import annotations

import time
import threading
from dataclasses import dataclass
from typing import Optional
from urllib.error import HTTPError, URLError

from proto.llm.client import ModelPressureWait, NETWORK_TIMEOUTS


@dataclass(frozen=True)
class ModelFailure:
    reason: str
    exception_type: str
    http_status: Optional[int] = None


def describe_model_failure(error):
    pending = [error]
    seen = set()
    network = None
    exception_type = type(error).__name__
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        exception_type = type(error).__name__
        if isinstance(error, ModelPressureWait):
            return ModelFailure(error.reason, exception_type)
        if isinstance(error, HTTPError):
            reason = {401: "model_unauthorized", 403: "model_forbidden", 404: "model_not_found",
                      429: "model_rate_limited"}.get(error.code, "model_http_error")
            if 500 <= error.code < 600:
                reason = "model_server_error"
            return ModelFailure(reason, exception_type, error.code)
        if isinstance(error, NETWORK_TIMEOUTS):
            return ModelFailure("model_timeout", exception_type)
        if isinstance(error, ConnectionError):
            return ModelFailure("model_unreachable", exception_type)
        if error.__cause__ is not None:
            pending.append(error.__cause__)
        if isinstance(error, URLError):
            network = ModelFailure("model_unreachable", exception_type)
            if isinstance(error.reason, BaseException):
                pending.append(error.reason)
    return network or ModelFailure("model_request_failed", exception_type)


def model_failure(error):
    return describe_model_failure(error).reason


TRANSIENT_MODEL_REASONS = frozenset({
    "circuit_breaker_open", "model_timeout", "model_unreachable", "model_rate_limited", "model_server_error",
    "model_group_unavailable",
})
GROUP_FAILOVER_REASONS = frozenset({
    "model_timeout", "model_unreachable", "model_rate_limited", "model_server_error",
    "model_unauthorized", "model_not_found",
})


def model_wait_reason(error):
    message = str(error)
    if message == "circuit_breaker_open":
        return message
    for reason in ("budget_exhausted", "fleet_budget_exhausted"):
        if message.startswith(reason + ":"):
            return reason
    return model_failure(error)


class CircuitBreaker:
    def __init__(self, fail_threshold: int = 3, cooldown_s: float = 600.0):
        self._threshold = fail_threshold
        self._cooldown = cooldown_s
        self._fails = 0
        self._opened_at = 0.0
        self._state = "closed"   # closed | open | half_open
        self._reason = ""
        self._lock = threading.Lock()
        self._generation = 0

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def snapshot(self) -> dict:
        with self._lock:
            retry_at = self._opened_at + self._cooldown if self._state == "open" else None
            return {"state": self._state, "retry_at": retry_at, "reason": self._reason,
                    "waiting": self._state == "half_open" or bool(retry_at and time.time() < retry_at)}

    def allow(self) -> bool:
        return self.acquire() is not None

    def acquire(self):
        with self._lock:
            if self._state == "closed":
                return self._generation
            if self._state == "open" and time.time() - self._opened_at >= self._cooldown:
                self._state = "half_open"
                return self._generation
            return None

    def record_success(self, *, permit=None) -> None:
        with self._lock:
            if permit is not None and permit != self._generation:
                return
            self._fails = 0
            self._state = "closed"
            self._reason = ""

    def record_failure(self, reason="model_request_failed", *, permit=None, immediate=False) -> None:
        with self._lock:
            if permit is not None and permit != self._generation:
                return
            self._fails += 1
            self._reason = reason
            if immediate or self._state == "half_open" or self._fails >= self._threshold:
                self._state = "open"
                self._opened_at = time.time()
                self._generation += 1
