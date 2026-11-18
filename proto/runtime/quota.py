# -*- coding: utf-8 -*-
"""M32: 24 小时自主白名单配额——窗口内 N 次免审批，页面可配。"""
from __future__ import annotations

import time
from typing import Callable, List

from proto.storage.store import DocumentStore


class AutoApproveQuota:
    def __init__(self, max_actions: int, window_s: float = 86400.0,
                 now: Callable[[], float] = time.time,
                 store: DocumentStore = None, doc_id: str = "main"):
        self.max_actions = max_actions
        self.window_s = window_s
        self._now = now
        self._history: List[float] = []
        self._store = store
        self._doc_id = doc_id
        if self._store is not None:
            raw = self._store.get("quota", doc_id) or {}
            persisted_max = raw.get("max_actions")
            if persisted_max is not None and max_actions <= 0:
                self.max_actions = int(persisted_max)
            self._history = [float(t) for t in raw.get("history", [])]

    def _save(self) -> None:
        if self._store is not None:
            self._store.put("quota", self._doc_id, {
                "max_actions": self.max_actions,
                "window_s": self.window_s,
                "history": list(self._history),
            })

    def set_max_actions(self, max_actions: int) -> None:
        self.max_actions = max(0, int(max_actions))
        self._save()

    def try_consume(self, at: float = None) -> bool:
        at = at if at is not None else self._now()
        self._history = [t for t in self._history if at - t < self.window_s]
        if len(self._history) >= self.max_actions:
            return False
        self._history.append(at)
        self._save()
        return True

    def remaining(self) -> int:
        now = self._now()
        self._history = [t for t in self._history if now - t < self.window_s]
        return max(0, self.max_actions - len(self._history))
