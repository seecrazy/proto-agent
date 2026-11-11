# -*- coding: utf-8 -*-
"""M4-①: 阻尼控制（设计 §7.5）。

连续 N 次主动提议被主人拒绝/忽略 → 对应欲望触发阈值上调（有界）；
任何一次被接受 → 计数重置。效果：越被无视，越少打扰；一旦重新被采纳即恢复。
"""
from __future__ import annotations

from typing import Dict

from proto.kernel.audit import AuditEvent, AuditSpine
from proto.state.agent_state import StateManager


class DampingController:
    def __init__(self, state_manager: StateManager, audit: AuditSpine,
                 consecutive: int = 3, bump: float = 0.05):
        self._mgr = state_manager
        self._audit = audit
        self._consecutive = consecutive
        self._bump = bump
        self._ignored: Dict[str, int] = {}

    def record_ignored(self, desire_dim: str) -> None:
        n = self._ignored.get(desire_dim, 0) + 1
        self._ignored[desire_dim] = n
        if n >= self._consecutive:
            new_threshold = self._mgr.adjust_desire_threshold(desire_dim, +self._bump)
            self._audit.append(AuditEvent.DAMPEN, {
                "desire": desire_dim, "ignored_streak": n,
                "new_threshold": new_threshold})
            self._ignored[desire_dim] = 0

    def record_accepted(self, desire_dim: str) -> None:
        self._ignored[desire_dim] = 0
