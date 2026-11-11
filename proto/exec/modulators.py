# -*- coding: utf-8 -*-
"""L4 情绪→认知调制器（设计 §7.4 表）。

全部为有界纯函数：情绪调制检索与决策偏置，不直接生成表达语气。
"""
from __future__ import annotations


def retrieval_breadth(base_k: int, arousal: float) -> int:
    """唤醒度调制策略检索广度 k，界 [base_k-2, base_k+3]。"""
    k = round(base_k * (1 + 0.6 * (arousal - 0.5)))
    return max(base_k - 2, min(base_k + 3, int(k)))


def exploration_epsilon(exploration: float, arousal: float) -> float:
    """探索比例 ε = 0.05 + 0.30×exploration×arousal，界 [0.05, 0.35]。"""
    eps = 0.05 + 0.30 * exploration * arousal
    return round(max(0.05, min(0.35, eps)), 4)


def valence_bias(pleasure: float) -> float:
    """检索效价偏置 = 0.2×pleasure，界 ±0.2。正值提升正向策略检索权重。"""
    return round(max(-0.2, min(0.2, 0.2 * pleasure)), 4)


def initiative_threshold(base_th: float, dominance: float) -> float:
    """支配度调制主动阈值 = base×(1.1−0.4×dominance)，界 ±20%×base。"""
    th = base_th * (1.1 - 0.4 * dominance)
    return round(max(base_th * 0.8, min(base_th * 1.2, th)), 4)
