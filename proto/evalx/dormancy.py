# -*- coding: utf-8 -*-
"""M4-④: dormant 冻结看门狗（设计 §6.2）。

A/B 验证规则落地：某参数的行为代理在快照对比中测不出可测差异
（无任何快照对 |d| ≥ 阈值）→ 冻结该参数，拒绝后续调制，直到主人解冻
或行为代理重新变得可测。
"""
from __future__ import annotations

from proto.evalx.harness import EvalReport
from proto.state.agent_state import StateManager


class DormancyWatchdog:
    def __init__(self, state_manager: StateManager, threshold: float = 0.5):
        self._mgr = state_manager
        self._threshold = threshold

    def inspect(self, param: str, proxy_metric: str, report: EvalReport) -> bool:
        """返回是否冻结。冻结条件：报告中该代理指标无任何可测快照对。"""
        measurable_pairs = report.measurable(metric=proxy_metric, threshold=self._threshold)
        if not measurable_pairs:
            self._mgr.freeze_dormant(
                param, reason=f"行为代理 {proxy_metric} 不可测（|d| < {self._threshold}）")
            return True
        return False
