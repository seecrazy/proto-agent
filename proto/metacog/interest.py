# -*- coding: utf-8 -*-
"""M4-④: 兴趣强化收敛（设计 §7.3 机制五）。

兴趣不是预设也不是主人指定的：记录每个领域的任务结果与主人反馈，
EMA 收敛出权重，供目标仲裁在排序时加权——高反馈领域自然胜出。
"""
from __future__ import annotations

from typing import Dict

DEFAULT_WEIGHT = 0.5


class InterestModel:
    def __init__(self, lr: float = 0.25):
        self._lr = lr
        self._weights: Dict[str, float] = {}

    def record(self, domain: str, success: bool, praise: bool = False) -> None:
        target = 1.0 if (success or praise) else 0.0
        # 主人点赞是更强的信号：学习率加倍
        lr = self._lr * 2 if praise else self._lr
        w = self._weights.get(domain, DEFAULT_WEIGHT)
        self._weights[domain] = round(w + lr * (target - w), 4)

    def weight(self, domain: str, default: float = DEFAULT_WEIGHT) -> float:
        return self._weights.get(domain, default)
