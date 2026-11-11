# -*- coding: utf-8 -*-
"""L3 规则内化台账——双向门（设计 §7.2，R2）。

external --(≥5 次成功且成功率 ≥0.8)--> internal
internal --(de_internalize / rollback)--> external（冷却 3 次应用）
"""
from __future__ import annotations

import time
import uuid
from typing import Dict, List, Optional

from proto.kernel.audit import AuditEvent, AuditSpine

INTERNALIZE_MIN_SUCCESSES = 5
INTERNALIZE_MIN_SUCCESS_RATE = 0.8
DEINTERNALIZE_COOLDOWN = 3


class RuleLedger:
    def __init__(self, audit: AuditSpine):
        self._audit = audit
        self._rules: Dict[str, dict] = {}

    def add(self, text: str) -> str:
        rid = uuid.uuid4().hex[:12]
        self._rules[rid] = {
            "id": rid, "text": text, "status": "external",
            "applications": [], "internalized_at": None,
            "cooldown_active": False, "apps_since_deinternalize": 0, "origin": "owner",
        }
        return rid

    def get(self, rule_id: str) -> dict:
        return self._rules[rule_id]

    def record_application(self, rule_id: str, context: str, success: bool) -> None:
        rule = self._rules[rule_id]
        rule["applications"].append({"ts": time.time(), "context": context, "success": success})
        if rule["cooldown_active"]:
            rule["apps_since_deinternalize"] += 1

    def check_internalize(self, rule_id: str) -> bool:
        rule = self._rules[rule_id]
        if rule["status"] != "external":
            return False
        # 冷却：降级后 COOLDOWN 次应用内的检查一律阻止（设计 §7.2）
        if rule["cooldown_active"] and rule["apps_since_deinternalize"] <= DEINTERNALIZE_COOLDOWN:
            return False
        apps = rule["applications"]
        successes = [a for a in apps if a["success"]]
        if len(successes) < INTERNALIZE_MIN_SUCCESSES:
            return False
        if len(successes) / len(apps) < INTERNALIZE_MIN_SUCCESS_RATE:
            return False
        rule["status"] = "internal"
        rule["internalized_at"] = time.time()
        rule["cooldown_active"] = False
        self._audit.append(AuditEvent.INTERNALIZE, {"rule_id": rule_id, "text": rule["text"]})
        return True

    def de_internalize(self, rule_id: str) -> None:
        rule = self._rules[rule_id]
        if rule["status"] != "internal":
            raise ValueError(f"规则 {rule_id} 不处于 internal 状态")
        rule["status"] = "external"
        rule["internalized_at"] = None
        rule["cooldown_active"] = True
        rule["apps_since_deinternalize"] = 0
        self._audit.append(AuditEvent.DE_INTERNALIZE, {"rule_id": rule_id})

    def rollback(self, since_ts: float) -> int:
        """参数回滚的级联：降级窗口内内化的全部规则（设计 §7.2）。"""
        count = 0
        for rule in self._rules.values():
            if rule["status"] == "internal" and (rule["internalized_at"] or 0) >= since_ts:
                rule["status"] = "external"
                rule["internalized_at"] = None
                rule["cooldown_active"] = True
                rule["apps_since_deinternalize"] = 0
                count += 1
        self._audit.append(AuditEvent.ROLLBACK, {"since_ts": since_ts, "downgraded": count})
        return count

    def internal_rules(self) -> List[dict]:
        return [r for r in self._rules.values() if r["status"] == "internal"]

    def get_all(self) -> List[dict]:
        return list(self._rules.values())

    def to_dict(self) -> dict:
        return {"rules": list(self._rules.values())}

    @classmethod
    def from_dict(cls, raw: dict, audit: AuditSpine) -> "RuleLedger":
        ledger = cls(audit)
        for rule in raw.get("rules", []):
            ledger._rules[rule["id"]] = dict(rule)
        return ledger
