# -*- coding: utf-8 -*-
"""L3 策略蒸馏库（设计 §7.3，R8）。

检索 = 关键词重叠 × utility；ε 概率用一个异簇策略替换末位槽对抗单一文化；
修剪按情境簇保留覆盖度配额。
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from random import Random
from typing import Dict, List, Optional

_WORD = re.compile(r"[A-Za-z0-9]+")


@dataclass
class Strategy:
    id: str
    text: str
    cluster: str
    utility: float = 0.5
    uses: int = 0
    valence: float = 0.0  # >0 正向经验，<0 教训


def _tokens(text: str) -> set:
    toks = set(_WORD.findall(text.lower()))
    toks.update(ch for ch in text if "\u4e00" <= ch <= "\u9fff")
    return toks


@dataclass
class StrategyLibrary:
    strategies: Dict[str, Strategy] = field(default_factory=dict)

    def add(self, text: str, cluster: str, utility: float = 0.5, valence: float = 0.0) -> Strategy:
        s = Strategy(id=uuid.uuid4().hex[:12], text=text, cluster=cluster,
                     utility=round(utility, 4), valence=valence)
        self.strategies[s.id] = s
        return s

    def _match_score(self, query: str, s: Strategy) -> float:
        overlap = len(_tokens(query) & _tokens(s.text))
        return overlap * s.utility

    def retrieve(self, query: str, k: int = 3, epsilon: float = 0.15,
                 rng: Optional[Random] = None) -> List[Strategy]:
        rng = rng or Random()
        ranked = sorted(self.strategies.values(),
                        key=lambda s: self._match_score(query, s), reverse=True)
        top = list(ranked[:k])
        if not top or epsilon <= 0:
            return top
        if rng.random() < epsilon:
            top_clusters = {s.cluster for s in top}
            others = [s for s in ranked[k:] if s.cluster not in top_clusters]
            if others:
                top[-1] = rng.choice(others)
        return top

    def update(self, strategy_id: str, success: bool, lr: float = 0.2) -> None:
        s = self.strategies[strategy_id]
        target = 1.0 if success else 0.0
        s.utility = round(s.utility + lr * (target - s.utility), 4)
        s.uses += 1
        s.valence = round(s.valence + (0.1 if success else -0.1), 4)

    def prune(self, min_utility: float = 0.25, keep_per_cluster: int = 3) -> int:
        removed = 0
        by_cluster: Dict[str, List[Strategy]] = {}
        for s in self.strategies.values():
            by_cluster.setdefault(s.cluster, []).append(s)
        for cluster, items in by_cluster.items():
            items.sort(key=lambda s: s.utility, reverse=True)
            survivors = items[:keep_per_cluster] + [s for s in items[keep_per_cluster:] if s.utility >= min_utility]
            for s in items:
                if s not in survivors:
                    del self.strategies[s.id]
                    removed += 1
        return removed
