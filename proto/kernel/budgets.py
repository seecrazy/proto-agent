# -*- coding: utf-8 -*-
"""L1 预算池与资源熔断（设计 §5）。

三分池：global（总闸）/ owner_tasks（主人任务预留）/ self_initiated（自主目标小额）。
任何消耗先过 global 再过所属池；global 超限触发熔断，熔断期间拒绝一切消耗。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, Optional


@dataclass
class BudgetDecision:
    granted: bool
    reason: str = ""


class BudgetPools:
    def __init__(self, config: Dict[str, Dict[str, float]]):
        if "global" not in config:
            raise ValueError("必须提供 global 池")
        self._config = config
        self._remaining: Dict[str, Dict[str, float]] = {
            name: dict(units) for name, units in config.items()
        }
        self.breaker_tripped: bool = False
        self.on_trip: Optional[Callable[[str], None]] = None  # 由 Kernel 接线到审计

    def _trip(self, reason: str) -> BudgetDecision:
        self.breaker_tripped = True
        if self.on_trip is not None:
            self.on_trip(reason)
        return BudgetDecision(False, reason)

    def try_consume(self, pool: str, amounts: Dict[str, float]) -> BudgetDecision:
        if self.breaker_tripped:
            return BudgetDecision(False, "breaker_tripped")
        if pool not in self._remaining:
            return BudgetDecision(False, f"unknown_pool:{pool}")
        for unit in amounts:
            if unit not in self._remaining["global"]:
                # 配置错误/打错的单位名：拒绝但不熔断（熔断是真实资源耗尽的语义）
                return BudgetDecision(False, f"unknown_unit:{unit}")
        for unit, amount in amounts.items():
            if self._remaining["global"][unit] < amount:
                return self._trip(f"global_shortfall:{unit}")
        for unit, amount in amounts.items():
            if pool != "global" and self._remaining[pool].get(unit, 0.0) < amount:
                return BudgetDecision(False, f"pool_shortfall:{pool}:{unit}")
        for unit, amount in amounts.items():
            self._remaining["global"][unit] -= amount
            if pool != "global":
                self._remaining[pool][unit] -= amount
        return BudgetDecision(True)

    def remaining(self, pool: str, unit: str) -> Optional[float]:
        if pool not in self._remaining:
            return None
        return self._remaining[pool].get(unit)

    def trip(self, reason: str = "manual") -> BudgetDecision:
        """手动熔断（Kernel 公开接口）。"""
        return self._trip(reason)

    def reset_cycle(self) -> None:
        self._remaining = {name: dict(units) for name, units in self._config.items()}
        self.breaker_tripped = False
