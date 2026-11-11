# -*- coding: utf-8 -*-
"""阶段 7：私有评分器（设计 §5 / §9.1）。

评分逻辑归 L1 侧持有；变体与进化引擎只拿分数数字，不拿评分规则，
无法针对评分器构造作弊输入（作弊面收敛到人类所有的测试集本身）。
"""
from __future__ import annotations

from proto.evolution.sandbox import SandboxResult


class PrivateScorer:
    def score(self, result: SandboxResult) -> float:
        if result.total == 0:
            return 0.0
        return round(result.passed / result.total, 4)
