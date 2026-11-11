# -*- coding: utf-8 -*-
"""阶段 7：变体档案库——开放式进化树（设计 §9.3，DGM 范式）。"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import List, Optional

from proto.storage.store import DocumentStore

STATUSES = ("candidate", "canary", "default", "rejected")


@dataclass
class Variant:
    id: str
    parent_id: Optional[str]
    code: str
    score: float
    status: str
    created_at: float


class VariantArchive:
    def __init__(self, store: DocumentStore, collection: str = "archive"):
        self._store = store
        self._collection = collection

    def add(self, code: str, parent_id: Optional[str], score: float, status: str) -> Variant:
        if status not in STATUSES:
            raise ValueError(f"非法状态: {status}")
        variant = Variant(id=uuid.uuid4().hex[:12], parent_id=parent_id, code=code,
                          score=score, status=status, created_at=time.time())
        self._store.put(self._collection, variant.id, {
            "id": variant.id, "parent_id": parent_id, "code": code,
            "score": score, "status": status, "created_at": variant.created_at,
        })
        return variant

    def get(self, variant_id: str) -> Optional[Variant]:
        raw = self._store.get(self._collection, variant_id)
        if raw is None:
            return None
        return Variant(**{k: raw[k] for k in
                          ("id", "parent_id", "code", "score", "status", "created_at")})

    def all(self) -> List[Variant]:
        rows = self._store.find(self._collection, lambda _d: True)
        return [self.get(r["id"]) for r in sorted(rows, key=lambda x: x["created_at"])]

    def best(self) -> Variant:
        return max(self.all(), key=lambda v: v.score)

    def default(self) -> Optional[Variant]:
        defaults = [v for v in self.all() if v.status == "default"]
        return max(defaults, key=lambda v: v.created_at) if defaults else None

    def lineage(self, variant_id: str) -> List[Variant]:
        chain = []
        current = self.get(variant_id)
        while current is not None:
            chain.append(current)
            current = self.get(current.parent_id) if current.parent_id else None
        return list(reversed(chain))

    def set_status(self, variant_id: str, status: str) -> None:
        if status not in STATUSES:
            raise ValueError(f"非法状态: {status}")
        raw = self._store.get(self._collection, variant_id)
        raw["status"] = status
        self._store.put(self._collection, variant_id, raw)
