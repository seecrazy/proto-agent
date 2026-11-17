# -*- coding: utf-8 -*-
"""M32: 24 小时自主白名单配额——窗口内 N 次免审批，页面可配。"""
from __future__ import annotations

import time
from typing import Callable, List


class AutoApproveQuota:
    def __init__(self, max_actions: int, window_s: float = 86400.0,
                 now: Callable[[], float] = time.time):
        self.max_actions = max_actions
        self.window_s = window_s
        self._now = now
        self._history: List[float] = []

    def try_consume(self, at: float = None) -> bool:
        at = at if at is not None else self._now()
        self._history = [t for t in self._history if at - t < self.window_s]
        if len(self._history) >= self.max_actions:
            return False
        self._history.append(at)
        return True

    def remaining(self) -> int:
        now = self._now()
        self._history = [t for t in self._history if now - t < self.window_s]
        return max(0, self.max_actions - len(self._history))
