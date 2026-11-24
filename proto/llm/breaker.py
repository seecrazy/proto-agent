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

from proto.llm.errors import ModelPressureWait, ModelRequestStopped, ModelResponseError, NETWORK_TIMEOUTS


@dataclass(frozen=True)
class ModelFailure:
    reason: str
    exception_type: str
    http_status: Optional[int] = None
    stage: Optional[str] = None
    provider_code: Optional[str] = None      # 供应方的业务错误码（只记码，不记消息）


def _attached(error, name):
    """异常实例上由我们附加的属性。不能对 HTTPError 用 getattr：它的 __getattr__
    对未知属性抛 KeyError 而不是 AttributeError。"""
    try:
        return vars(error).get(name)
    except TypeError:
        return None


def describe_model_failure(error):
    pending = [error]
    seen = set()
    network = None
    exception_type = type(error).__name__
    provider_code = None
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        exception_type = type(error).__name__
        provider_code = _attached(error, "provider_code") or provider_code
        if isinstance(error, ModelPressureWait):
            return ModelFailure(error.reason, exception_type, provider_code=provider_code)
        if isinstance(error, ModelRequestStopped):
            return ModelFailure(str(error), exception_type, provider_code=provider_code)
        if isinstance(error, ModelResponseError):
            return ModelFailure(error.reason, exception_type, error.http_status, error.stage, provider_code)
        if isinstance(error, HTTPError):
            reason = {401: "model_unauthorized", 403: "model_forbidden", 404: "model_not_found",
                      429: "model_rate_limited"}.get(error.code, "model_http_error")
            if 500 <= error.code < 600:
                reason = "model_server_error"
            return ModelFailure(reason, exception_type, error.code, "headers", provider_code)
        if isinstance(error, NETWORK_TIMEOUTS):
            return ModelFailure("model_timeout", exception_type, provider_code=provider_code)
        if isinstance(error, ConnectionError):
            return ModelFailure("model_unreachable", exception_type, provider_code=provider_code)
        if error.__cause__ is not None:
            pending.append(error.__cause__)
        if isinstance(error, URLError):
            network = ModelFailure("model_unreachable", exception_type, provider_code=provider_code)
            if isinstance(error.reason, BaseException):
                pending.append(error.reason)
    return network or ModelFailure("model_request_failed", exception_type, provider_code=provider_code)


def model_failure(error):
    return describe_model_failure(error).reason


TRANSIENT_MODEL_REASONS = frozenset({
    "circuit_breaker_open", "model_timeout", "model_unreachable", "model_rate_limited", "model_server_error",
    "model_group_unavailable", "model_stream_interrupted", "model_invalid_response",
    # 输出超上限后循环已切到小步决策（compact_decisions），马上重试即可；此前被归为不可重试，
    # 任务停下来等 10 分钟自动恢复，界面显示「模型或额度受阻」（2026-11-24 生产实证 3 次）
    "model_output_truncated",
})
GROUP_FAILOVER_REASONS = frozenset({
    "model_timeout", "model_unreachable", "model_rate_limited", "model_server_error",
    "model_unauthorized", "model_not_found", "model_stream_interrupted", "model_invalid_response",
    # 这个接入的额度用完了，换下一个接入是正当的；但它不是瞬时限流，不进 TRANSIENT——
    # 全组都用尽时任务停下来等主人，而不是带着几天后的重试时刻悄悄排队
    "model_quota_exhausted",
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

    def release(self, permit):
        with self._lock:
            if self._generation == permit and self._state == "half_open":
                self._state = "open"

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
