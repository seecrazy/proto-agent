# -*- coding: utf-8 -*-
"""阶段 7：进化测试集治理（设计 §9.1，R3）。

铁律：人类所有、只增不删、变体不可提案。
- 追加用例必须持 OWNER 通道签名消息（HMAC 认证），任何其他来源一律 PermissionError；
- 本类按构造即治理（governance by construction）：不存在任何删除/修改用例的方法。
"""
from __future__ import annotations

import uuid
from typing import List, Optional

from proto.kernel.anchor import OwnerAnchor
from proto.kernel.audit import AuditEvent, AuditSpine
from proto.storage.store import DocumentStore


class OwnedTestSet:
    def __init__(self, store: DocumentStore, anchor: OwnerAnchor, audit: AuditSpine,
                 collection: str = "testset"):
        self._store = store
        self._anchor = anchor
        self._audit = audit
        self._collection = collection

    def add_case(self, case_input, expected, owner_message: Optional[dict]) -> str:
        if owner_message is None or not self._anchor.verify(owner_message).ok:
            raise PermissionError("测试集只接受 OWNER 通道签名追加（人类所有，变体不可提案）")
        payload = owner_message["payload"]
        case_input = case_input if case_input is not None else payload.get("input")
        expected = expected if expected is not None else payload.get("expected")
        case_id = uuid.uuid4().hex[:12]
        self._store.put(self._collection, case_id, {
            "id": case_id, "input": case_input, "expected": expected,
        })
        self._audit.append(AuditEvent.TEST_SET_APPEND, {"case_id": case_id})
        return case_id

    def cases(self) -> List[dict]:
        return sorted(self._store.find(self._collection, lambda _d: True),
                      key=lambda c: c["id"])
