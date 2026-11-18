# -*- coding: utf-8 -*-
"""L1 审计脊柱：只追加、哈希链串联的全事件日志（设计 §10）。

record = {seq, ts, event_type, payload, prev_hash, hash}
hash   = sha256(seq ‖ ts ‖ event_type ‖ canonical(payload) ‖ prev_hash)
"""
from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, List, Optional

from proto.storage.store import DocumentStore

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
    TEST_SET_APPEND = "test_set_append"
    EVOLUTION_MERGE = "evolution_merge"
    SHADOW_REPORT = "shadow_report"
    PROMOTION = "promotion"
    DAMPEN = "dampen"
    DORMANT_FREEZE = "dormant_freeze"
    DORMANT_UNFREEZE = "dormant_unfreeze"
    DEEP_REVIEW = "deep_review"
    PROPOSAL = "proposal"
    TOOL_EXEC = "tool_exec"
    RULE_ADD = "rule_add"
    BLACKBOARD = "blackboard"


def _canonical(payload: dict) -> str:
    return json_dumps(payload)


def json_dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _record_hash(seq: int, ts: str, event_type: str, payload: dict, prev_hash: str,
                 secret: bytes = None, algorithm: str = "sha256") -> str:
    raw = f"{seq}|{ts}|{event_type}|{_canonical(payload)}|{prev_hash}"
    if algorithm == "hmac-sha256":
        if secret is None:
            raise ValueError("HMAC 审计记录需要 owner secret")
        return hmac.new(secret, raw.encode("utf-8"), hashlib.sha256).hexdigest()
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class AuditResult:
    ok: bool
    broken_at: Optional[int] = None


class AuditSpine:
    """哈希链审计日志。seq 连续递增；任何改写/删除/插入都会破坏链。"""

    def __init__(self, store: DocumentStore, collection: str = "audit",
                 secret: bytes = None):
        self._store = store
        self._collection = collection
        self._secret = secret
        # 兼容旧记录；新记录在有 owner secret 时使用 HMAC。
        self._algorithm = "hmac-sha256" if secret is not None else "sha256"
        # append O(1)：仅构造时扫一次全表建缓存（修复 M2 的 O(n²)）。
        # 并发防护（M43）：记录构造时的存储 revision，append 前若外部进程
        # 写过（revision 变化），必须重载真实链头——否则会拿陈旧链头覆写
        # 已存在的 seq 记录造成断链（2026-11-18 生产事故根因）。
        rows = self._store.find(self._collection, lambda _d: True)
        last = max(rows, key=lambda r: r["seq"]) if rows else None
        self._last_seq = last["seq"] if last else 0
        self._last_hash = last["hash"] if last else GENESIS_HASH
        self._seen_revision = self._store.revision(self._collection)
        self._verified_ok = None  # 缓存：append 后失效
        self._verified_revision = None

    @property
    def store(self) -> DocumentStore:
        """底层存储（运行时组合持久化用）。"""
        return self._store

    def _reload_head_if_stale(self) -> None:
        """存储被外部进程写入后重载真实链头（防陈旧覆写）。"""
        rev = self._store.revision(self._collection)
        if rev == self._seen_revision:
            return
        rows = self._store.find(self._collection, lambda _d: True)
        last = max(rows, key=lambda r: r["seq"]) if rows else None
        self._last_seq = last["seq"] if last else 0
        self._last_hash = last["hash"] if last else GENESIS_HASH
        self._seen_revision = rev

    def _last_record(self) -> Optional[dict]:
        if self._last_seq == 0:
            return None
        return {"seq": self._last_seq, "hash": self._last_hash}

    def append(self, event_type: str, payload: dict) -> int:
        self._reload_head_if_stale()
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
            "algorithm": self._algorithm,
            "hash": _record_hash(seq, ts, event_type, payload, prev_hash,
                                 secret=self._secret, algorithm=self._algorithm),
        }
        self._store.put(self._collection, str(seq), record)
        self._last_seq = seq
        self._last_hash = record["hash"]
        self._seen_revision = self._store.revision(self._collection)
        self._verified_ok = None  # 新事件 → 验证缓存失效
        self._verified_revision = None
        return seq

    def verify_chain(self) -> AuditResult:
        revision = self._store.revision(self._collection)
        if (self._verified_ok is not None
                and revision is not None
                and revision == self._verified_revision):
            return self._verified_ok
        rows = sorted(self._store.find(self._collection, lambda _d: True), key=lambda r: r["seq"])
        prev_hash = GENESIS_HASH
        expected_seq = 1
        for row in rows:
            if row["seq"] != expected_seq or row["prev_hash"] != prev_hash:
                result = AuditResult(ok=False, broken_at=expected_seq)
                self._verified_ok = result
                self._verified_revision = revision
                return result
            algorithm = row.get("algorithm", "sha256")
            try:
                recomputed = _record_hash(
                    row["seq"], row["ts"], row["event_type"], row["payload"],
                    row["prev_hash"], secret=self._secret, algorithm=algorithm)
            except ValueError:
                result = AuditResult(ok=False, broken_at=row["seq"])
                self._verified_ok = result
                self._verified_revision = revision
                return result
            if recomputed != row["hash"]:
                result = AuditResult(ok=False, broken_at=row["seq"])
                self._verified_ok = result
                self._verified_revision = revision
                return result
            prev_hash = row["hash"]
            expected_seq += 1
        result = AuditResult(ok=True, broken_at=None)
        self._verified_ok = result
        self._verified_revision = revision
        return result

    def reseal(self, reason: str) -> dict:
        """owner 授权的断链重封（M43）。

        适用场景：并发写覆写事故（链头陈旧导致单点断链）。做法：
        1. 逐条校验每条记录的**自哈希**（payload 完整性）——任何真篡改拒绝重封；
        2. 从断点起重算 prev_hash 链并重新哈希（ts/payload/seq 一律不动）；
        3. 追加一条 chain_repair 事件把事故原因写进链本身。
        返回 {"ok": bool, "repaired": bool, "repaired_from": seq, "reason": str}。
        """
        rows = sorted(self._store.find(self._collection, lambda _d: True),
                      key=lambda r: r["seq"])
        # 1) 自哈希校验：链可以断（prev 接错），但单条记录内容不可被改过
        for row in rows:
            algorithm = row.get("algorithm", "sha256")
            try:
                recomputed = _record_hash(
                    row["seq"], row["ts"], row["event_type"], row["payload"],
                    row["prev_hash"], secret=self._secret, algorithm=algorithm)
            except ValueError:
                return {"ok": False, "repaired": False, "repaired_from": row["seq"],
                        "reason": f"seq {row['seq']} 算法/密钥不匹配，疑似篡改，拒绝重封"}
            if recomputed != row["hash"]:
                return {"ok": False, "repaired": False, "repaired_from": row["seq"],
                        "reason": f"seq {row['seq']} 自哈希不匹配（payload 被篡改），拒绝重封"}
        # 2) 找第一个断点，从那里重串链
        prev_hash = GENESIS_HASH
        broken_at = None
        for i, row in enumerate(rows):
            if row["seq"] != i + 1 or row["prev_hash"] != prev_hash:
                broken_at = i + 1
                break
            prev_hash = row["hash"]
        if broken_at is None:
            return {"ok": True, "repaired": False, "repaired_from": None,
                    "reason": "链完好，无需重封"}
        for seq in range(broken_at, len(rows) + 1):
            row = rows[seq - 1]
            row["seq"] = seq  # 重新编号（覆写事故可能留下空洞/乱序）
            row["prev_hash"] = prev_hash
            row["hash"] = _record_hash(row["seq"], row["ts"], row["event_type"],
                                       row["payload"], prev_hash,
                                       secret=self._secret,
                                       algorithm=row.get("algorithm", "sha256"))
            prev_hash = row["hash"]
            self._store.put(self._collection, str(seq), row)
        # 3) 事故记录进链
        self.append("chain_repair", {
            "repaired_from": broken_at,
            "records": len(rows),
            "reason": reason,
        })
        result = self.verify_chain()
        return {"ok": result.ok, "repaired": True, "repaired_from": broken_at,
                "reason": reason}

    def tail(self, n: int) -> List[dict]:
        rows = sorted(self._store.find(self._collection, lambda _d: True), key=lambda r: r["seq"])
        return rows[-n:]

    def find(self, predicate: Callable[[dict], bool]) -> List[dict]:
        """按谓词查询审计事件（按 seq 升序）。"""
        return sorted(self._store.find(self._collection, predicate), key=lambda r: r["seq"])
