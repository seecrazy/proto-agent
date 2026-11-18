# -*- coding: utf-8 -*-
"""L1 主人锚定与三通道认证（设计 §3）。

信任分级：
  OWNER   认证通道（HMAC 签名），反馈权重 1.0
  SESSION 主人交互轨迹推断的隐式反馈，权重 0.3
  CONTENT 外部内容，权重 0.05，累积池另有 50% 硬顶（见 modulation.py）
"""
from __future__ import annotations

import enum
import hashlib
import hmac
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from proto.kernel.audit import AuditEvent, AuditSpine
from proto.storage.store import DocumentStore

FRESHNESS_WINDOW_S = 300.0
_NONCE_CACHE_LIMIT = 1024


class Channel(enum.Enum):
    OWNER = "owner"
    SESSION = "session"
    CONTENT = "content"


SOURCE_WEIGHT: Dict[Channel, float] = {
    Channel.OWNER: 1.0,
    Channel.SESSION: 0.3,
    Channel.CONTENT: 0.05,
}


@dataclass
class VerifyResult:
    ok: bool
    reason: str = ""


def _canonical(payload) -> str:
    import json
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hmac_hex(secret: bytes, payload: dict, ts: float, nonce: str) -> str:
    msg = f"{_canonical(payload)}|{ts}|{nonce}".encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def fingerprint_of(secret: bytes) -> str:
    return "sha256:" + hashlib.sha256(secret).hexdigest()


class OwnerAnchor:
    """OWNER 通道的密钥锚定。指纹与通道绑定写入内核后不可被调制更改。"""

    def __init__(self, secret: bytes, fingerprint: str, audit: AuditSpine,
                 store: DocumentStore = None, key_path=None):
        self._secret = secret
        self.fingerprint = fingerprint
        self._audit = audit
        self._recent_nonces: List[str] = []
        self._store = store
        self._key_path = key_path

    @classmethod
    def enroll(cls, store: DocumentStore, audit: AuditSpine, secret: bytes,
               recovery: bool = False) -> "OwnerAnchor":
        """注册仪式：生成锚定记录并写审计。已有锚定时拒绝静默替换（设计 §3.2
        "锚定不可重写"）——恢复须显式 recovery=True（线下仪式），且审计记录被替换指纹。"""
        existing = store.get("anchor", "owner")
        if existing is not None and not recovery:
            raise PermissionError("锚定已存在；恢复需显式 recovery=True（线下仪式）")
        anchor = cls(secret, fingerprint_of(secret), audit, store=store)
        record = {
            "channel": Channel.OWNER.value,
            "fingerprint": anchor.fingerprint,
            "created_at": time.time(),
        }
        if existing is not None:
            record["replaced"] = existing["fingerprint"]
        store.put("anchor", "owner", record)
        audit.append(AuditEvent.ANCHOR_INIT, {
            "fingerprint": anchor.fingerprint,
            **({"replaced": existing["fingerprint"]} if existing is not None else {}),
        })
        return anchor

    @staticmethod
    def _sign_with(secret: bytes, payload: dict, ts: float, nonce: str) -> dict:
        return {
            "channel": Channel.OWNER.value,
            "payload": payload,
            "ts": ts,
            "nonce": nonce,
            "sig": _hmac_hex(secret, payload, ts, nonce),
        }

    def sign(self, payload: dict, ts: float, nonce: str) -> dict:
        return self._sign_with(self._secret, payload, ts, nonce)

    def verify(self, message) -> VerifyResult:
        """认证边界：任何畸形输入一律拒绝（malformed），绝不抛异常。结构先于语义。"""
        malformed = VerifyResult(False, "malformed")
        if not isinstance(message, dict):
            return malformed
        payload = message.get("payload")
        ts = message.get("ts")
        nonce = message.get("nonce")
        sig = message.get("sig")
        if not isinstance(payload, dict):
            return malformed
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            return malformed
        if not isinstance(nonce, str) or not isinstance(sig, str):
            return malformed
        if message.get("channel") != Channel.OWNER.value:
            return VerifyResult(False, "not_owner_channel")
        if abs(time.time() - ts) > FRESHNESS_WINDOW_S:
            return VerifyResult(False, "stale_timestamp")
        expected = _hmac_hex(self._secret, payload, ts, nonce)
        if not hmac.compare_digest(expected, sig):
            return VerifyResult(False, "bad_signature")
        if nonce in self._recent_nonces:
            return VerifyResult(False, "nonce_replay")
        nonce_id = hashlib.sha256(nonce.encode("utf-8")).hexdigest()
        if self._store is not None:
            old = self._store.get("anchor_nonce", nonce_id)
            if old is not None:
                return VerifyResult(False, "nonce_replay")
        self._recent_nonces.append(nonce)
        if self._store is not None:
            self._store.put("anchor_nonce", nonce_id,
                            {"id": nonce_id, "ts": time.time()})
            if len(self._recent_nonces) % 256 == 0:
                cutoff = time.time() - FRESHNESS_WINDOW_S
                for row in self._store.find(
                        "anchor_nonce", lambda d: float(d.get("ts", 0)) < cutoff):
                    if row.get("id"):
                        self._store.delete("anchor_nonce", row["id"])
        if len(self._recent_nonces) > _NONCE_CACHE_LIMIT:
            self._recent_nonces = self._recent_nonces[-_NONCE_CACHE_LIMIT // 2:]
        return VerifyResult(True)

    def rotate(self, new_secret: bytes, countersigned_by_old: dict) -> None:
        """密钥轮换：旧钥会签 {rotate_to: 新指纹} 方可生效（设计 §3.2）。"""
        expected = _hmac_hex(
            self._secret,
            countersigned_by_old["payload"],
            countersigned_by_old["ts"],
            countersigned_by_old["nonce"],
        )
        if not hmac.compare_digest(expected, countersigned_by_old.get("sig", "")):
            raise PermissionError("轮换缺少有效旧钥会签")
        if countersigned_by_old["payload"].get("rotate_to") != fingerprint_of(new_secret):
            raise PermissionError("会签目标指纹不匹配")
        if abs(time.time() - countersigned_by_old["ts"]) > FRESHNESS_WINDOW_S:
            raise PermissionError("会签消息过期（捕获的旧会签不可重放）")
        old_fingerprint = self.fingerprint
        self._secret = new_secret
        self.fingerprint = fingerprint_of(new_secret)
        if self._store is not None:
            record = self._store.get("anchor", "owner") or {}
            record.update({
                "channel": Channel.OWNER.value,
                "fingerprint": self.fingerprint,
                "replaced": old_fingerprint,
                "rotated_at": time.time(),
            })
            self._store.put("anchor", "owner", record)
        if self._key_path is not None:
            key_path = Path(self._key_path)
            tmp = key_path.with_suffix(key_path.suffix + f".{os.getpid()}.tmp")
            tmp.write_text(new_secret.hex(), encoding="utf-8")
            tmp.chmod(stat.S_IRUSR | stat.S_IWUSR)
            os.replace(tmp, key_path)
        self._audit.append(AuditEvent.KEY_ROTATE, {"from": old_fingerprint, "to": self.fingerprint})
