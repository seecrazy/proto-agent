# -*- coding: utf-8 -*-
"""M19-R3: LLM 熔断器——防重试风暴（生产可靠性模式）。

连续 N 次失败 → 打开（cooldown 秒内不再调用 LLM，直接走离线回退）；
冷却期满 → 半开（放一次试探）；成功 → 关闭。三态机。
"""
from __future__ import annotations

import time


class CircuitBreaker:
    def __init__(self, fail_threshold: int = 3, cooldown_s: float = 600.0):
        self._threshold = fail_threshold
        self._cooldown = cooldown_s
        self._fails = 0
        self._opened_at = 0.0
        self._state = "closed"   # closed | open | half_open

    @property
    def state(self) -> str:
        return self._state

    def allow(self) -> bool:
        if self._state == "closed":
            return True
        if self._state == "open":
            if time.time() - self._opened_at >= self._cooldown:
                self._state = "half_open"
                return True
            return False
        return True   # half_open：放一次试探

    def record_success(self) -> None:
        self._fails = 0
        self._state = "closed"

    def record_failure(self) -> None:
        self._fails += 1
        if self._state == "half_open" or self._fails >= self._threshold:
            self._state = "open"
            self._opened_at = time.time()
