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
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from proto.kernel.audit import AuditEvent, AuditSpine
from proto.state.store import DocumentStore

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

    def __init__(self, secret: bytes, fingerprint: str, audit: AuditSpine):
        self._secret = secret
        self.fingerprint = fingerprint
        self._audit = audit
        self._recent_nonces: List[str] = []

    @classmethod
    def enroll(cls, store: DocumentStore, audit: AuditSpine, secret: bytes) -> "OwnerAnchor":
        """注册仪式：生成锚定记录并写审计。重复 enroll 会得到不同指纹的平行锚，
        verify 只认自己的钥——即同一存储可存在多个 Agent 实例互不冒充。"""
        anchor = cls(secret, fingerprint_of(secret), audit)
        existing = store.get("anchor", "owner")
        record = {
            "channel": Channel.OWNER.value,
            "fingerprint": anchor.fingerprint,
            "created_at": time.time(),
        }
        if existing is not None:
            record["replaced"] = existing["fingerprint"]
        store.put("anchor", "owner", record)
        audit.append(AuditEvent.ANCHOR_INIT, {"fingerprint": anchor.fingerprint})
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

    def verify(self, message: dict) -> VerifyResult:
        if message.get("channel") != Channel.OWNER.value:
            return VerifyResult(False, "not_owner_channel")
        if abs(time.time() - message.get("ts", 0.0)) > FRESHNESS_WINDOW_S:
            return VerifyResult(False, "stale_timestamp")
        expected = _hmac_hex(self._secret, message.get("payload", {}), message["ts"], message["nonce"])
        if not hmac.compare_digest(expected, message.get("sig", "")):
            return VerifyResult(False, "bad_signature")
        if message["nonce"] in self._recent_nonces:
            return VerifyResult(False, "nonce_replay")
        self._recent_nonces.append(message["nonce"])
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
        old_fingerprint = self.fingerprint
        self._secret = new_secret
        self.fingerprint = fingerprint_of(new_secret)
        self._audit.append(AuditEvent.KEY_ROTATE, {"from": old_fingerprint, "to": self.fingerprint})
