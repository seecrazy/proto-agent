# -*- coding: utf-8 -*-
"""M13: 记忆流（情景/语义/反思）——Generative Agents 记忆架构的零依赖实现。

检索得分 = 0.5×相关度 + 0.3×时近度 + 0.2×重要度
- 相关度：查询与记忆的关键词重叠（CJK 单字 + 英文词）
- 时近度：exp 衰减（半衰期 72h）；事实/反思有时近度下限——
  "你叫小明"不该因为是一周前记的就淡忘
- 重要度：写入时评定（主人夸奖/任务成败/反思默认高）

遗忘：不删除，检索分随时间衰减、被强化（reinforce）则回升——
像人一样"想不起来"而非"硬盘清空"。
"""
from __future__ import annotations

import math
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from proto.storage.store import DocumentStore

KINDS = ("episode", "fact", "reflection")
_WORD = re.compile(r"[A-Za-z0-9]+")
HALF_LIFE_H = 72.0
W_RELEVANCE, W_RECENCY, W_IMPORTANCE = 0.5, 0.3, 0.2
RECENCY_FLOOR = {"fact": 0.5, "reflection": 0.4}


def _tokens(text: str) -> set:
    toks = set(_WORD.findall(text.lower()))
    toks.update(ch for ch in text if "\u4e00" <= ch <= "\u9fff")
    return toks


@dataclass
class Memory:
    id: str
    kind: str
    text: str
    ts: float
    importance: float = 0.5
    last_access: float = 0.0
    source: str = "chat"

    def to_dict(self) -> dict:
        return vars(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "Memory":
        return cls(**{k: raw[k] for k in
                     ("id", "kind", "text", "ts", "importance", "last_access", "source")})


class MemoryStream:
    def __init__(self, store: DocumentStore, collection: str = "memory"):
        self._store = store
        self._collection = collection
        self._memories: Dict[str, Memory] = {
            m.id: m for m in (Memory.from_dict(r) for r in
                              store.find(collection, lambda _d: True))}

    def __len__(self) -> int:
        return len(self._memories)

    def add(self, kind: str, text: str, importance: float = 0.5,
            ts: float = None, source: str = "chat") -> Memory:
        if kind not in KINDS:
            raise ValueError(f"未知记忆类型: {kind}")
        now = ts if ts is not None else time.time()
        memory = Memory(id=uuid.uuid4().hex[:12], kind=kind, text=text[:400],
                         ts=now, importance=max(0.0, min(1.0, importance)),
                         last_access=now, source=source)
        self._memories[memory.id] = memory
        self._store.put(self._collection, memory.id, memory.to_dict())
        return memory

    def find_similar(self, text: str, threshold: float = 0.8) -> Optional[Memory]:
        """高重叠的既有记忆（去重/强化用）。"""
        query = _tokens(text)
        if not query:
            return None
        best, best_score = None, 0.0
        for m in self._memories.values():
            overlap = len(query & _tokens(m.text)) / max(1, len(query | _tokens(m.text)))
            if overlap > best_score:
                best, best_score = m, overlap
        return best if best_score >= threshold else None

    def reinforce(self, memory_id: str, ts: float = None) -> None:
        m = self._memories.get(memory_id)
        if m is None:
            return
        m.last_access = ts if ts is not None else time.time()
        self._store.put(self._collection, m.id, m.to_dict())

    def _recency(self, m: Memory, now: float) -> float:
        age_h = max(0.0, (now - m.last_access) / 3600.0)
        score = math.exp(-math.log(2) * age_h / HALF_LIFE_H)
        return max(score, RECENCY_FLOOR.get(m.kind, 0.0))

    def retrieve(self, query: str, k: int = 5, now: float = None) -> List[Memory]:
        now = now if now is not None else time.time()
        q = _tokens(query)
        scored = []
        for m in self._memories.values():
            toks = _tokens(m.text)
            union = len(q | toks)
            relevance = (len(q & toks) / union) if q and union else 0.0
            score = (W_RELEVANCE * relevance
                     + W_RECENCY * self._recency(m, now)
                     + W_IMPORTANCE * m.importance)
            scored.append((score, m))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [m for _s, m in scored[:k]]

    def episodes(self) -> List[Memory]:
        return sorted((m for m in self._memories.values() if m.kind == "episode"),
                      key=lambda m: m.ts)

    def facts(self) -> List[Memory]:
        return sorted((m for m in self._memories.values() if m.kind == "fact"),
                      key=lambda m: m.ts)

    def reflections(self) -> List[Memory]:
        return sorted((m for m in self._memories.values() if m.kind == "reflection"),
                      key=lambda m: m.ts)

    def snapshot(self) -> dict:
        return {"total": len(self._memories),
                "episodes": sum(1 for m in self._memories.values()
                                if m.kind == "episode"),
                "facts": [m.text for m in self.facts()],
                "reflections": [m.text for m in self.reflections()]}
