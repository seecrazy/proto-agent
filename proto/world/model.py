# -*- coding: utf-8 -*-
"""M20-②: 世界模型——L3 自主性的第二支柱。

按主题持续维护：带来源与时间戳的**事实**，以及**可检验的预测**
（陈述/复核期限/置信度/判定结果）。预测到期复核产生误差时，
由运行时触发好奇上抬与自主目标生成（第三支柱）。
"""
from __future__ import annotations

import time
import uuid
import hashlib
from typing import Dict, List, Optional

from proto.storage.store import DocumentStore

PREDICT_PROMPT = (
    "你是 PROTO 的世界模型。基于下面的学习材料，对相关领域做出 1-3 条"
    "**可检验的预测**（有明确时限、可用未来观察判定）。"
    '只输出 JSON 数组：[{"statement": "...", "days_ahead": 30, '
    '"confidence": 0.0~1.0}]。'
)

VERDICT_PROMPT = (
    "你是 PROTO 的预测复核器。对照最新观察材料，判定下面的预测当前应判定为："
    'correct（已被支持）/ wrong（已被反驳）/ unknown（尚无法判定）。'
    '只输出 JSON：{"verdict": "...", "evidence": "一句话依据"}。'
)


class WorldModel:
    def __init__(self, store: DocumentStore, collection: str = "worldmodel"):
        self._store = store
        self._collection = collection

    @staticmethod
    def _topic_id(name: str) -> str:
        return "t" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]

    def _topic(self, name: str) -> dict:
        doc_id = self._topic_id(name)
        doc = self._store.get(self._collection, doc_id)
        if doc is not None:
            return doc
        # 兼容旧版本以原 topic 作为 doc_id 的存储。
        try:
            legacy = self._store.get(self._collection, name)
        except ValueError:
            legacy = None
        return legacy or {"name": name, "facts": [], "predictions": []}

    def _save_topic(self, name: str, doc: dict) -> None:
        doc["name"] = name
        doc["id"] = self._topic_id(name)
        self._store.put(self._collection, doc["id"], doc)

    def add_fact(self, topic: str, text: str, source: str) -> None:
        doc = self._topic(topic)
        doc["facts"].append({"text": text[:300], "source": source,
                             "ts": time.time()})
        doc["facts"] = doc["facts"][-50:]
        self._save_topic(topic, doc)

    def record_prediction(self, topic: str, statement: str,
                          deadline_ts: float, confidence: float = 0.5,
                          created_at: float = None) -> str:
        doc = self._topic(topic)
        pid = uuid.uuid4().hex[:12]
        doc["predictions"].append({
            "id": pid, "statement": statement[:300],
            "deadline_ts": deadline_ts,
            "confidence": round(max(0.0, min(1.0, confidence)), 4),
            "status": "open",
            "created_at": created_at if created_at is not None else time.time(),
            "checked_at": None, "evidence": ""})
        self._save_topic(topic, doc)
        return pid

    def due_predictions(self, now: float = None,
                        limit: int = 2) -> List[dict]:
        now = now if now is not None else time.time()
        due = []
        for doc in self._store.find(self._collection, lambda _d: True):
            for p in doc["predictions"]:
                if p["status"] == "open" and p["deadline_ts"] <= now:
                    due.append({"topic": doc["name"], **p})
        return sorted(due, key=lambda p: p["deadline_ts"])[:limit]

    def settle(self, prediction_id: str, verdict: str,
               evidence: str = "") -> Optional[dict]:
        for doc in self._store.find(self._collection, lambda _d: True):
            for p in doc["predictions"]:
                if p["id"] == prediction_id:
                    p["status"] = verdict
                    p["checked_at"] = time.time()
                    p["evidence"] = evidence[:300]
                    self._save_topic(doc["name"], doc)
                    return p
        return None

    def _advance_all(self, seconds: float) -> None:
        """测试辅助：所有 open 预测的期限整体前移（模拟时间流逝）。"""
        for doc in self._store.find(self._collection, lambda _d: True):
            changed = False
            for p in doc["predictions"]:
                if p["status"] == "open":
                    p["deadline_ts"] -= seconds
                    changed = True
            if changed:
                self._save_topic(doc["name"], doc)

    def snapshot(self) -> dict:
        topics = {}
        for doc in self._store.find(self._collection, lambda _d: True):
            topics[doc["name"]] = {
                "facts": doc["facts"][-5:],
                "predictions": doc["predictions"][-10:]}
        return {"topics": topics}
