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
from proto.storage.store import DocumentStore


@dataclass
class ActionProposal:
    category: ActionCategory
    description: str
    payload: dict
    task_id: str
    self_initiated: bool = False
    desire_dim: Optional[str] = None  # 自主动作关联的欲望维度（阻尼控制用）
    proposal_id: Optional[str] = None  # 入队时由队列回填，供外部寻址

    def to_dict(self) -> dict:
        return {
            "category": self.category.value,
            "description": self.description,
            "payload": self.payload,
            "task_id": self.task_id,
            "self_initiated": self.self_initiated,
            "desire_dim": self.desire_dim,
            "proposal_id": self.proposal_id,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "ActionProposal":
        return cls(
            category=ActionCategory(raw["category"]),
            description=raw.get("description", ""),
            payload=raw.get("payload") or {},
            task_id=raw.get("task_id", ""),
            self_initiated=bool(raw.get("self_initiated", False)),
            desire_dim=raw.get("desire_dim"),
            proposal_id=raw.get("proposal_id"),
        )


@dataclass
class ApprovalRecord:
    proposal_id: str
    granted: bool
    description: str = ""
    decided_at: float = 0.0
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "proposal_id": self.proposal_id, "granted": self.granted,
            "description": self.description, "decided_at": self.decided_at,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "ApprovalRecord":
        return cls(
            proposal_id=raw.get("proposal_id", ""),
            granted=bool(raw.get("granted", False)),
            description=raw.get("description", ""),
            decided_at=float(raw.get("decided_at", 0.0)),
            reason=raw.get("reason", ""),
        )


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
                 damping=None, store: DocumentStore = None, auto_quota=None):
        self.kernel = kernel
        self._audit = audit
        self.daily_cap = daily_cap
        self._damping = damping  # DampingController：拒绝/接受回报给 L3 阻尼
        self._pending: Dict[str, _Pending] = {}
        self._decisions: Dict[str, ApprovalRecord] = {}
        self._store = store
        self.auto_quota = auto_quota
        import threading as _th
        self._lock = _th.RLock()   # M50：链 submit 与主人 decide 并发安全
        self._today = time.strftime("%Y-%m-%d")
        self._self_initiated_today = 0
        self._load()

    def _load(self) -> None:
        if self._store is None:
            return
        raw = self._store.get("approvals", "main") or {}
        self._today = raw.get("today", self._today)
        self._self_initiated_today = int(raw.get("self_initiated_today", 0))
        for row in raw.get("pending", []):
            try:
                proposal = ActionProposal.from_dict(row["proposal"])
                pid = row["proposal_id"]
            except (KeyError, TypeError, ValueError):
                continue
            self._pending[pid] = _Pending(
                proposal=proposal,
                proposal_id=pid,
                submitted_at=float(row.get("submitted_at", time.time())))
        for row in raw.get("decisions", []):
            try:
                record = ApprovalRecord.from_dict(row)
            except (TypeError, ValueError):
                continue
            self._decisions[record.proposal_id] = record

    def _save(self) -> None:
        if self._store is None:
            return
        self._store.put("approvals", "main", {
            "today": self._today,
            "self_initiated_today": self._self_initiated_today,
            "pending": [{
                "proposal_id": p.proposal_id,
                "submitted_at": p.submitted_at,
                "proposal": p.proposal.to_dict(),
            } for p in self._pending.values()],
            "decisions": [r.to_dict() for r in list(self._decisions.values())[-200:]],
        })

    def _roll_day(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self._today:
            self._today = today
            self._self_initiated_today = 0
            self._save()

    def _submit_unlocked(self, proposal: ActionProposal) -> Submission:
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
        if (proposal.self_initiated and decision.requires_human
                and proposal.category is ActionCategory.VISIBLE_WRITE
                and self.auto_quota is not None
                and self.auto_quota.try_consume()):
            record = ApprovalRecord(
                proposal_id=pid, granted=True,
                description=proposal.description, decided_at=time.time(),
                reason="auto_quota")
            self._decisions[pid] = record
            self._self_initiated_today += 1
            self._audit.append(AuditEvent.APPROVAL, {
                "description": proposal.description,
                "category": proposal.category.value,
                "self_initiated": True,
                "path": "auto_quota",
            })
            self._save()
            return Submission(pid, True, approval=record)
        self._pending[pid] = _Pending(proposal=proposal, proposal_id=pid)
        if proposal.self_initiated:
            self._self_initiated_today += 1
        self._audit.append(AuditEvent.PROPOSAL, {
            "proposal_id": pid,
            "description": proposal.description,
            "category": proposal.category.value,
            "self_initiated": proposal.self_initiated,
        })
        self._save()
        return Submission(pid, False)

    def pending(self) -> List[ActionProposal]:
        return [p.proposal for p in sorted(self._pending.values(), key=lambda x: x.submitted_at)]

    def pending_with_ts(self) -> List[tuple]:
        """[(proposal, submitted_at)]——主人超时自主裁决用。"""
        return [(p.proposal, p.submitted_at)
                for p in sorted(self._pending.values(), key=lambda x: x.submitted_at)]

    def _notify_damping(self, proposal: ActionProposal, accepted: bool) -> None:
        if (self._damping is not None and proposal.self_initiated
                and proposal.desire_dim):
            if accepted:
                self._damping.record_accepted(proposal.desire_dim)
            else:
                self._damping.record_ignored(proposal.desire_dim)

    def _approve_unlocked(self, proposal_id: str) -> ApprovalRecord:
        if proposal_id in self._decisions:
            return self._decisions[proposal_id]
        pending = self._pending.pop(proposal_id)
        self._notify_damping(pending.proposal, accepted=True)
        record = ApprovalRecord(proposal_id=proposal_id, granted=True,
                                description=pending.proposal.description,
                                decided_at=time.time())
        self._audit.append(AuditEvent.APPROVAL, {
            "description": pending.proposal.description,
            "category": pending.proposal.category.value,
            "self_initiated": pending.proposal.self_initiated})
        self._decisions[proposal_id] = record
        self._save()
        return record

    def _deny_unlocked(self, proposal_id: str, reason: str) -> ApprovalRecord:
        if proposal_id in self._decisions:
            return self._decisions[proposal_id]
        pending = self._pending.pop(proposal_id)
        self._notify_damping(pending.proposal, accepted=False)
        record = ApprovalRecord(proposal_id=proposal_id, granted=False,
                                description=pending.proposal.description,
                                decided_at=time.time(), reason=reason)
        self._audit.append(AuditEvent.DENIAL, {
            "description": pending.proposal.description,
            "reason": reason, "category": pending.proposal.category.value})
        self._decisions[proposal_id] = record
        self._save()
        return record


# ---- M50 资源锁包装 ----
def _locked_queue_op(cls, name):
    orig = getattr(cls, f"_{name}_unlocked")

    def wrapper(self, *args, **kwargs):
        with self._lock:
            return orig(self, *args, **kwargs)

    setattr(cls, name, wrapper)


for _n in ("submit", "approve", "deny"):
    _locked_queue_op(ApprovalQueue, _n)
