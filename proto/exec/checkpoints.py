# -*- coding: utf-8 -*-
"""阶段 6：任务检查点（设计 §8 / §9.3）。

每个检查点绑定 schema_version@variant_id——旧检查点由产生它的变体分支续跑，
升级后断点续传不断（R3 的运维细节落地）。
"""
from __future__ import annotations

from typing import Optional

from proto.storage.store import DocumentStore

SCHEMA_VERSION = 2


class CheckpointStore:
    def __init__(self, store: DocumentStore, collection: str = "checkpoints"):
        self._store = store
        self._collection = collection

    def put(self, task_id: str, state: dict, variant_id: str = "core@1.0") -> None:
        self._store.put(self._collection, task_id, {
            "task_id": task_id,
            "schema_version": SCHEMA_VERSION,
            "variant_id": variant_id,
            "state": state,
        })

    def resume(self, task_id: str, expect_variant: Optional[str] = None) -> Optional[dict]:
        """返回检查点记录；expect_variant 不匹配时抛错——由产生它的变体续跑。"""
        record = self._store.get(self._collection, task_id)
        if record is None:
            return None
        if expect_variant is not None and record["variant_id"] != expect_variant:
            raise ValueError(f"检查点属于 {record['variant_id']}，不能由 {expect_variant} 续跑")
        return record
