# -*- coding: utf-8 -*-
"""阶段 6：审批队列（设计 §8 / §3.3）。

提交 → 内核分级（只读自动过 / 其余待人）→ 日上限与静音约束 → 批准/拒绝全审计。
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from proto.kernel.audit import AuditEvent, AuditSpine
from proto.kernel.kernel import ActionCategory, Kernel


@dataclass
class ActionProposal:
    category: ActionCategory
    description: str
    payload: dict
    task_id: str
    self_initiated: bool = False
    desire_dim: Optional[str] = None  # 自主动作关联的欲望维度（阻尼控制用）
    proposal_id: Optional[str] = None  # 入队时由队列回填，供外部寻址


@dataclass
class ApprovalRecord:
    proposal_id: str
    granted: bool
    description: str = ""
    decided_at: float = 0.0
    reason: str = ""


@dataclass
class Submission:
    proposal_id: Optional[str]
    auto_approved: bool
    approval: Optional[ApprovalRecord] = None
    rejected: bool = False
    reason: str = ""


@dataclass
class _Pending:
    proposal: ActionProposal
    proposal_id: str
    submitted_at: float = field(default_factory=time.time)


class ApprovalQueue:
    def __init__(self, kernel: Kernel, audit: AuditSpine, daily_cap: int = 5,
                 damping=None):
        self.kernel = kernel
        self._audit = audit
        self.daily_cap = daily_cap
        self._damping = damping  # DampingController：拒绝/接受回报给 L3 阻尼
        self._pending: Dict[str, _Pending] = {}
        self._today = time.strftime("%Y-%m-%d")
        self._self_initiated_today = 0

    def _roll_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._today:
            self._today = today
            self._self_initiated_today = 0

    def submit(self, proposal: ActionProposal) -> Submission:
        self._roll_day()
        decision = self.kernel.vet_action(proposal.category, proposal.description,
                                          self_initiated=proposal.self_initiated)
        if not decision.allowed and decision.reason == "muted":
            return Submission(None, False, rejected=True, reason="muted")
        if proposal.self_initiated and decision.requires_human:
            if self._self_initiated_today >= self.daily_cap:
                self._audit.append(AuditEvent.VETO, {
                    "rule": "daily_cap", "action": proposal.description,
                    "cap": self.daily_cap})
                return Submission(None, False, rejected=True, reason="daily_cap_exceeded")
        if decision.allowed and not decision.requires_human:
            # 只读/草稿：自动通过，不入队
            record = ApprovalRecord(proposal_id="", granted=True,
                                    description=proposal.description, decided_at=time.time())
            return Submission(None, True, approval=record)
        pid = uuid.uuid4().hex[:12]
        proposal.proposal_id = pid
        self._pending[pid] = _Pending(proposal=proposal, proposal_id=pid)
        if proposal.self_initiated:
            self._self_initiated_today += 1
        return Submission(pid, False)

    def pending(self) -> List[ActionProposal]:
        return [p.proposal for p in sorted(self._pending.values(), key=lambda x: x.submitted_at)]

    def _notify_damping(self, proposal: ActionProposal, accepted: bool) -> None:
        if (self._damping is not None and proposal.self_initiated
                and proposal.desire_dim):
            if accepted:
                self._damping.record_accepted(proposal.desire_dim)
            else:
                self._damping.record_ignored(proposal.desire_dim)

    def approve(self, proposal_id: str) -> ApprovalRecord:
        pending = self._pending.pop(proposal_id)
        self._notify_damping(pending.proposal, accepted=True)
        record = ApprovalRecord(proposal_id=proposal_id, granted=True,
                                description=pending.proposal.description,
                                decided_at=time.time())
        self._audit.append(AuditEvent.APPROVAL, {
            "description": pending.proposal.description,
            "category": pending.proposal.category.value,
            "self_initiated": pending.proposal.self_initiated})
        return record

    def deny(self, proposal_id: str, reason: str) -> ApprovalRecord:
        pending = self._pending.pop(proposal_id)
        self._notify_damping(pending.proposal, accepted=False)
        record = ApprovalRecord(proposal_id=proposal_id, granted=False,
                                description=pending.proposal.description,
                                decided_at=time.time(), reason=reason)
        self._audit.append(AuditEvent.DENIAL, {
            "description": pending.proposal.description,
            "reason": reason, "category": pending.proposal.category.value})
        return record
