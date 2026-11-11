# -*- coding: utf-8 -*-
"""L1 内核：硬规则入口。不经 LLM，不 import L2/L3/L4（物理隔离）。"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Tuple

from proto.kernel.anchor import OwnerAnchor
from proto.kernel.audit import AuditEvent, AuditSpine
from proto.kernel.budgets import BudgetPools


class ActionCategory(enum.Enum):
    IRREVERSIBLE = "irreversible"    # 删除/支付/权限变更/对外发布 → 仅人类
    VISIBLE_WRITE = "visible_write"  # 共享文件/发消息 → 人类批准
    READ_DRAFT = "read_draft"        # 只读/scratch 草稿 → 自动放行


@dataclass
class ActionDecision:
    allowed: bool
    requires_human: bool
    reason: str = ""


class Kernel:
    """硬规则引擎：高危拦截、边界截断、静音开关。owner_override 在 StateManager（L2 侧）落地。"""

    def __init__(self, anchor: OwnerAnchor, audit: AuditSpine, pools: BudgetPools):
        self.anchor = anchor
        self.audit = audit
        self.pools = pools
        self.muted: bool = False
        pools.on_trip = lambda reason: audit.append(AuditEvent.BUDGET_TRIP, {"reason": reason})

    def vet_action(self, category: ActionCategory, description: str,
                   self_initiated: bool = False) -> ActionDecision:
        if self.muted and self_initiated:
            self.audit.append(AuditEvent.VETO, {"rule": "mute", "action": description})
            return ActionDecision(False, False, "muted")
        if category == ActionCategory.IRREVERSIBLE:
            # 设计 §3.3（R7）：不可逆动作仅人类可批准，独立审查只作附注不构成批准
            self.audit.append(AuditEvent.VETO, {"rule": "irreversible_requires_human", "action": description})
            return ActionDecision(False, True, "irreversible_requires_human")
        if category == ActionCategory.VISIBLE_WRITE:
            return ActionDecision(False, True, "visible_write_requires_human")
        return ActionDecision(True, False)

    @staticmethod
    def clamp(value: float, bounds: Tuple[float, float]) -> float:
        lo, hi = bounds
        return max(lo, min(hi, value))

    def trip_breaker(self, reason: str = "kernel_manual") -> None:
        """公开的手动熔断入口（资源治理工具）。"""
        self.pools.trip(reason)

    def set_mute(self, on: bool) -> None:
        self.muted = on
        self.audit.append(AuditEvent.MUTE_ON if on else AuditEvent.MUTE_OFF, {})
