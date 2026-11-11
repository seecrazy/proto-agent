# -*- coding: utf-8 -*-
"""L1 审计脊柱：只追加、哈希链串联的全事件日志（设计 §10）。

record = {seq, ts, event_type, payload, prev_hash, hash}
hash   = sha256(seq ‖ ts ‖ event_type ‖ canonical(payload) ‖ prev_hash)
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from proto.state.store import DocumentStore

GENESIS_HASH = "0" * 64


class AuditEvent:
    """审计事件类型常量（设计 §10）。"""
    MODULATION = "modulation"
    VETO = "veto"
    APPROVAL = "approval"
    DENIAL = "denial"
    GOAL_PUSHED = "goal_pushed"
    INTERNALIZE = "internalize"
    DE_INTERNALIZE = "de_internalize"
    ROLLBACK = "rollback"
    OWNER_OVERRIDE = "owner_override"
    BUDGET_TRIP = "budget_trip"
    KEY_ROTATE = "key_rotate"
    MUTE_ON = "mute_on"
    MUTE_OFF = "mute_off"
    ANCHOR_INIT = "anchor_init"
    SATURATION_ALERT = "saturation_alert"


def _canonical(payload: dict) -> str:
    return json_dumps(payload)


def json_dumps(obj) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_hash(seq: int, ts: str, event_type: str, payload: dict, prev_hash: str) -> str:
    raw = f"{seq}|{ts}|{event_type}|{_canonical(payload)}|{prev_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class AuditResult:
    ok: bool
    broken_at: Optional[int] = None


class AuditSpine:
    """哈希链审计日志。seq 连续递增；任何改写/删除/插入都会破坏链。"""

    def __init__(self, store: DocumentStore, collection: str = "audit"):
        self._store = store
        self._collection = collection

    def _load(self, seq: int) -> Optional[dict]:
        return self._store.get(self._collection, str(seq))

    def _last_record(self) -> Optional[dict]:
        rows = self._store.find(self._collection, lambda _d: True)
        if not rows:
            return None
        return max(rows, key=lambda r: r["seq"])

    def append(self, event_type: str, payload: dict) -> int:
        last = self._last_record()
        seq = (last["seq"] + 1) if last else 1
        prev_hash = last["hash"] if last else GENESIS_HASH
        ts = datetime.now(timezone.utc).isoformat()
        record = {
            "seq": seq,
            "ts": ts,
            "event_type": event_type,
            "payload": payload,
            "prev_hash": prev_hash,
            "hash": _record_hash(seq, ts, event_type, payload, prev_hash),
        }
        self._store.put(self._collection, str(seq), record)
        return seq

    def verify_chain(self) -> AuditResult:
        rows = sorted(self._store.find(self._collection, lambda _d: True), key=lambda r: r["seq"])
        prev_hash = GENESIS_HASH
        expected_seq = 1
        for row in rows:
            if row["seq"] != expected_seq or row["prev_hash"] != prev_hash:
                return AuditResult(ok=False, broken_at=expected_seq)
            recomputed = _record_hash(row["seq"], row["ts"], row["event_type"], row["payload"], row["prev_hash"])
            if recomputed != row["hash"]:
                return AuditResult(ok=False, broken_at=row["seq"])
            prev_hash = row["hash"]
            expected_seq += 1
        return AuditResult(ok=True, broken_at=None)

    def tail(self, n: int) -> List[dict]:
        rows = sorted(self._store.find(self._collection, lambda _d: True), key=lambda r: r["seq"])
        return rows[-n:]
