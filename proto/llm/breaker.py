# -*- coding: utf-8 -*-
"""M19-R3: LLM 熔断器——防重试风暴（生产可靠性模式）。

连续 N 次失败 → 打开（cooldown 秒内不再调用 LLM，直接走离线回退）；
冷却期满 → 半开（放一次试探）；成功 → 关闭。三态机。
"""
from __future__ import annotations

import time
import threading
from urllib.error import HTTPError, URLError


def model_failure(error):
    while error is not None:
        if isinstance(error, HTTPError):
            return {401: "model_unauthorized", 403: "model_forbidden",
                    429: "model_rate_limited"}.get(error.code, "model_http_error")
        if isinstance(error, TimeoutError):
            return "model_timeout"
        if isinstance(error, URLError) and not error.__cause__:
            return "model_timeout" if isinstance(error.reason, TimeoutError) else "model_unreachable"
        error = error.__cause__
    return "model_request_failed"


TRANSIENT_MODEL_REASONS = frozenset({
    "circuit_breaker_open", "model_timeout", "model_unreachable", "model_rate_limited", "model_server_error",
})


def model_wait_reason(error):
    message = str(error)
    if message == "circuit_breaker_open":
        return message
    for reason in ("budget_exhausted", "fleet_budget_exhausted"):
        if message.startswith(reason + ":"):
            return reason
    cause = error
    while cause is not None:
        if isinstance(cause, HTTPError) and 500 <= cause.code < 600:
            return "model_server_error"
        cause = cause.__cause__
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

    def record_failure(self, reason="model_request_failed", *, permit=None) -> None:
        with self._lock:
            if permit is not None and permit != self._generation:
                return
            self._fails += 1
            self._reason = reason
            if self._state == "half_open" or self._fails >= self._threshold:
                self._state = "open"
                self._opened_at = time.time()
                self._generation += 1
