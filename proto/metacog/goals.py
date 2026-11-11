# -*- coding: utf-8 -*-
"""L3 目标生成与仲裁（设计 §7.5，R6）。

score = 0.35×owner_alignment + 0.25×feasibility + 0.25×gap_norm + 0.15×interest
主人对齐是第一权重；主人指派目标绕过仲裁并消耗 owner_tasks 预算池；
自主目标必须过 self_initiated 小额预算池，池空只进"待考虑"。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from proto.kernel.audit import AuditEvent, AuditSpine
from proto.kernel.kernel import Kernel

WEIGHTS = {"alignment": 0.35, "feasibility": 0.25, "gap": 0.25, "interest": 0.15}
_WORD = re.compile(r"[A-Za-z0-9]+")


def _tokens(text: str) -> set:
    toks = set(_WORD.findall(text.lower()))
    toks.update(ch for ch in text if "\u4e00" <= ch <= "\u9fff")
    return toks


def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class Goal:
    description: str
    desire_dim: str
    gap: float
    feasibility: float
    interest: float
    verification: str

    def __post_init__(self):
        # 设计 §7.5 硬约束：每个目标必须能被外部验证
        if not self.verification or not self.verification.strip():
            raise ValueError("目标必须附带可验证完成标准")


@dataclass
class ArbitratedGoal:
    goal: Goal
    score: float
    breakdown: Dict[str, float] = field(default_factory=dict)


@dataclass
class PushDecision:
    pushed: bool
    reason: str = ""
    queued_for_consideration: bool = False


class GoalArbiter:
    def __init__(self, kernel: Kernel, owner_priority: str = ""):
        self._kernel = kernel
        self.owner_priority = owner_priority
        self.goal_stack: List[Goal] = []
        self.consideration_queue: List[Goal] = []

    def score_goal(self, goal: Goal) -> ArbitratedGoal:
        alignment = _jaccard(goal.description, self.owner_priority)
        breakdown = {
            "alignment": round(alignment, 4),
            "feasibility": round(goal.feasibility, 4),
            "gap": round(goal.gap, 4),
            "interest": round(goal.interest, 4),
        }
        score = (WEIGHTS["alignment"] * alignment
                 + WEIGHTS["feasibility"] * goal.feasibility
                 + WEIGHTS["gap"] * goal.gap
                 + WEIGHTS["interest"] * goal.interest)
        return ArbitratedGoal(goal=goal, score=round(score, 4), breakdown=breakdown)

    def arbitrate(self, candidates: List[Goal]) -> ArbitratedGoal:
        if not candidates:
            raise ValueError("候选为空")
        winner = max((self.score_goal(g) for g in candidates), key=lambda a: a.score)
        return winner

    def _audit_push(self, goal: Goal, source: str) -> None:
        self._kernel.audit.append(AuditEvent.GOAL_PUSHED, {
            "description": goal.description, "source": source,
            "verification": goal.verification,
        })

    def push_owner_goal(self, goal: Goal, cost: Optional[Dict[str, float]] = None) -> PushDecision:
        """主人指派：绕过仲裁；消耗 owner_tasks 预留池。"""
        decision = self._kernel.pools.try_consume("owner_tasks", cost or {})
        if not decision.granted:
            self._kernel.audit.append(AuditEvent.DENIAL, {
                "goal": goal.description, "source": "owner_assigned", "reason": decision.reason})
            return PushDecision(False, decision.reason)
        self.goal_stack.append(goal)
        self._audit_push(goal, "owner_assigned")
        return PushDecision(True)

    def push_self_initiated(self, goal: Goal, cost: Optional[Dict[str, float]] = None) -> PushDecision:
        """自主目标：必须过 self_initiated 小额池；池空进待考虑（设计 §7.5）。"""
        decision = self._kernel.pools.try_consume("self_initiated", cost or {})
        if not decision.granted:
            self.consideration_queue.append(goal)
            self._kernel.audit.append(AuditEvent.DENIAL, {
                "goal": goal.description, "source": "self_initiated",
                "reason": "self_initiated_pool_exhausted",
                "queued_for_consideration": True})
            return PushDecision(False, "self_initiated_pool_exhausted", queued_for_consideration=True)
        self.goal_stack.append(goal)
        self._audit_push(goal, "self_initiated")
        return PushDecision(True)
