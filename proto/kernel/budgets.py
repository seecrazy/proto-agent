# -*- coding: utf-8 -*-
"""L1 预算池与资源熔断（设计 §5）。

三分池：global（总闸）/ owner_tasks（主人任务预留）/ self_initiated（自主目标小额）。
任何消耗先过 global 再过所属池；global 超限触发熔断，熔断期间拒绝一切消耗。
"""
from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import date
from typing import Callable, Dict, Optional

from proto.storage.store import DocumentStore


_UNIT_SUFFIX = {"k": 1e3, "w": 1e4, "万": 1e4, "m": 1e6, "兆": 1e6}


def parse_amount(text) -> float:
    """数值+单位解析（M52）：'8k'→8000、'16万'→160000、'1.2M'→1200000、
    '200000'→200000。非法/负数返回 None。"""
    if text is None:
        return None
    s = str(text).strip().lower().replace("，", "").replace(",", "")
    if not s:
        return None
    mult = 1.0
    if s[-1] in _UNIT_SUFFIX:
        mult = _UNIT_SUFFIX[s[-1]]
        s = s[:-1].strip()
    try:
        value = float(s) * mult
    except ValueError:
        return None
    return value if math.isfinite(value) and value > 0 else None


def _checked_amounts(amounts: Dict[str, float]) -> Dict[str, float]:
    checked = {}
    for unit, amount in amounts.items():
        if isinstance(amount, bool):
            raise ValueError(f"invalid_amount:{unit}")
        try:
            value = float(amount)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"invalid_amount:{unit}") from exc
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"invalid_amount:{unit}")
        checked[unit] = value
    return checked


@dataclass
class BudgetDecision:
    granted: bool
    reason: str = ""


class BudgetPools:
    def __init__(self, config: Dict[str, Dict[str, float]],
                 store: DocumentStore = None, doc_id: str = "main"):
        if "global" not in config:
            raise ValueError("必须提供 global 池")
        self._config = {name: _checked_amounts(units)
                        for name, units in config.items()}
        self._store = store
        self._doc_id = doc_id
        self._cycle_date = date.today().isoformat()
        self._remaining: Dict[str, Dict[str, float]] = {
            name: dict(units) for name, units in self._config.items()
        }
        self.breaker_tripped: bool = False
        self.on_trip: Optional[Callable[[str], None]] = None  # 由 Kernel 接线到审计
        self._lock = threading.RLock()   # M50：资源级锁（链消耗与主人充值并发安全）
        self._load()

    def _load(self) -> None:
        if self._store is None:
            return
        raw = self._store.get("kernel_budget", self._doc_id) or {}
        today = date.today().isoformat()
        # M52：控制台改过的上限优先于 env/默认（持久化，重启保持）
        saved_caps = raw.get("caps")
        if isinstance(saved_caps, dict):
            for name, units in saved_caps.items():
                if name in self._config and isinstance(units, dict):
                    for unit, amount in units.items():
                        if unit in self._config[name]:
                            try:
                                self._config[name].update(_checked_amounts({unit: amount}))
                            except ValueError:
                                continue
        self._remaining = {name: dict(units)
                           for name, units in self._config.items()}
        saved_date = raw.get("cycle_date")
        if saved_date == today and isinstance(raw.get("remaining"), dict):
            for name, units in self._remaining.items():
                saved_units = raw["remaining"].get(name, {})
                if not isinstance(saved_units, dict):
                    continue
                for unit in units:
                    try:
                        value = float(saved_units.get(unit, units[unit]))
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if math.isfinite(value):
                        units[unit] = min(value, self._config[name][unit])
            self._cycle_date = saved_date
            self.breaker_tripped = bool(raw.get("breaker_tripped", False))

    def _save(self) -> None:
        if self._store is not None:
            self._store.put("kernel_budget", self._doc_id, {
                "cycle_date": self._cycle_date,
                "remaining": {name: dict(units)
                              for name, units in self._remaining.items()},
                "caps": {name: dict(units)
                         for name, units in self._config.items()},
                "breaker_tripped": self.breaker_tripped,
            })

    def remaining_all(self) -> dict:
        with self._lock:
            self.ensure_cycle()
            return {name: dict(units) for name, units in self._remaining.items()}

    def set_cap(self, pool: str, unit: str, value: float) -> None:
        """M52：主人直改某池某单位上限（持久化）。立即生效语义：
        上调→余量同步补到新上限；下调→余量夹紧到新上限。"""
        self.set_caps({pool: {unit: value}})

    def set_caps(self, caps: Dict[str, Dict[str, float]]) -> None:
        """全量校验后原子更新一组上限。"""
        checked = {pool: _checked_amounts(units) for pool, units in caps.items()}
        with self._lock:
            for pool, units in checked.items():
                for unit in units:
                    if pool not in self._config or unit not in self._config[pool]:
                        raise KeyError(f"unknown_pool_or_unit:{pool}.{unit}")
            self.ensure_cycle()
            for pool, units in checked.items():
                for unit, value in units.items():
                    old = self._config[pool][unit]
                    self._config[pool][unit] = value
                    current = self._remaining[pool].get(unit, 0.0)
                    self._remaining[pool][unit] = value if value > old else min(current, value)
            self._save()

    def refill(self) -> None:
        """主人显式充值：今日各池恢复到配置上限（审计由调用方记）。"""
        with self._lock:
            self.ensure_cycle()
            self._remaining = {name: dict(units)
                               for name, units in self._config.items()}
            self.breaker_tripped = False
            self._save()

    def ensure_cycle(self) -> None:
        with self._lock:
            today = date.today().isoformat()
            if today != self._cycle_date:
                self._remaining = {
                    name: dict(units) for name, units in self._config.items()}
                self.breaker_tripped = False
                self._cycle_date = today
                self._save()

    def _trip(self, reason: str) -> BudgetDecision:
        self.breaker_tripped = True
        self._save()
        if self.on_trip is not None:
            self.on_trip(reason)
        return BudgetDecision(False, reason)

    def try_consume(self, pool: str,
                    amounts: Dict[str, float]) -> BudgetDecision:
        with self._lock:
            return self._try_consume_locked(pool, amounts)

    def _try_consume_locked(self, pool: str,
                            amounts: Dict[str, float]) -> BudgetDecision:
        self.ensure_cycle()
        if self.breaker_tripped:
            return BudgetDecision(False, "breaker_tripped")
        if pool not in self._remaining:
            return BudgetDecision(False, f"unknown_pool:{pool}")
        try:
            amounts = _checked_amounts(amounts)
        except (AttributeError, ValueError):
            return BudgetDecision(False, "invalid_amount")
        for unit in amounts:
            if unit not in self._remaining["global"]:
                # 配置错误/打错的单位名：拒绝但不熔断（熔断是真实资源耗尽的语义）
                return BudgetDecision(False, f"unknown_unit:{unit}")
        for unit, amount in amounts.items():
            if self._remaining["global"][unit] < amount:
                return self._trip(f"global_shortfall:{unit}")
        for unit, amount in amounts.items():
            if (pool != "global" and unit in self._remaining[pool]
                    and self._remaining[pool][unit] < amount):
                return BudgetDecision(False, f"pool_shortfall:{pool}:{unit}")
        for unit, amount in amounts.items():
            self._remaining["global"][unit] -= amount
            if pool != "global" and unit in self._remaining[pool]:
                self._remaining[pool][unit] -= amount
        self._save()
        return BudgetDecision(True)

    def refund(self, pool: str, amounts: Dict[str, float]) -> None:
        """任务提前暂停/失败时退回未执行步骤的预留预算。"""
        amounts = _checked_amounts(amounts)
        with self._lock:
            self.ensure_cycle()
            self._validate_units(pool, amounts)
            for name in dict.fromkeys(("global", pool)):
                for unit, amount in amounts.items():
                    if unit in self._remaining[name]:
                        self._remaining[name][unit] = min(
                            self._config[name][unit], self._remaining[name][unit] + amount)
            self._save()

    def _validate_units(self, pool: str, amounts: Dict[str, float]) -> None:
        if pool not in self._remaining:
            raise KeyError(f"unknown_pool:{pool}")
        for unit in amounts:
            if unit not in self._remaining["global"]:
                raise KeyError(f"unknown_unit:{unit}")

    def settle(self, pool: str, reserved: Dict[str, float],
               actual: Dict[str, float]) -> None:
        """结算已发生的用量，超额形成欠额，不因预算不足而漏记。"""
        reserved, actual = _checked_amounts(reserved), _checked_amounts(actual)
        units = set(reserved) | set(actual)
        with self._lock:
            self.ensure_cycle()
            self._validate_units(pool, dict.fromkeys(units))
            for name in dict.fromkeys(("global", pool)):
                for unit in units:
                    if unit in self._remaining[name]:
                        delta = actual.get(unit, 0.0) - reserved.get(unit, 0.0)
                        self._remaining[name][unit] = min(
                            self._config[name][unit], self._remaining[name][unit] - delta)
            if any(value < 0 for value in self._remaining["global"].values()):
                self._trip("global_actual_usage_exceeded")
            else:
                self._save()

    def remaining(self, pool: str, unit: str) -> Optional[float]:
        with self._lock:
            self.ensure_cycle()
            if pool not in self._remaining:
                return None
            return self._remaining[pool].get(unit)

    def trip(self, reason: str = "manual") -> BudgetDecision:
        """手动熔断（Kernel 公开接口）。"""
        with self._lock:
            return self._trip(reason)

    def reset_cycle(self) -> None:
        with self._lock:
            self._remaining = {name: dict(units) for name, units in self._config.items()}
            self.breaker_tripped = False
            self._cycle_date = date.today().isoformat()
            self._save()
