# -*- coding: utf-8 -*-
"""M32: Agent 间协作——共享黑板（发帖/认领/回复），控制台可观测。"""
from __future__ import annotations

import time
import uuid
from typing import List, Optional

from proto.storage.store import DocumentStore


class Blackboard:
    """舰队共享黑板：求助帖 → 认领 → 回复。全审计在各自 spine，这里存协作内容。"""

    def __init__(self, store: DocumentStore, collection: str = "blackboard"):
        self._store = store
        self._collection = collection

    def post(self, from_agent: str, text: str, kind: str = "help") -> str:
        nid = uuid.uuid4().hex[:12]
        self._store.put(self._collection, nid, {
            "id": nid, "kind": kind, "from": from_agent, "text": text[:500],
            "ts": time.time(), "claimed_by": None, "replies": []})
        return nid

    def read(self, kind: Optional[str] = None) -> List[dict]:
        rows = self._store.find(self._collection, lambda _d: True)
        rows = sorted(rows, key=lambda r: r["ts"], reverse=True)
        return [r for r in rows if kind is None or r["kind"] == kind]

    def claim(self, note_id: str, by_agent: str) -> None:
        doc = self._store.get(self._collection, note_id)
        if doc and not doc.get("claimed_by"):
            doc["claimed_by"] = by_agent
            self._store.put(self._collection, note_id, doc)

    def reply(self, note_id: str, from_agent: str, text: str) -> None:
        doc = self._store.get(self._collection, note_id)
        if doc:
            doc["replies"].append({"from": from_agent, "text": text[:500],
                                   "ts": time.time()})
            self._store.put(self._collection, note_id, doc)

    def open_questions(self) -> List[dict]:
        return [r for r in self.read() if r["kind"] == "help"
                and not r.get("claimed_by")]
