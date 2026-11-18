# -*- coding: utf-8 -*-
"""M4-④: 欲望动态增长（设计 §6.1）。

缺口不只是衰减：长时间无主人交互 → 社交连接缺口上升；闲置 → 好奇缺口上升。
有界、可观测；触发阈值由阻尼控制调制。写回经 StateManager 落盘（单一写者）。
"""
from __future__ import annotations

from proto.state.agent_state import StateManager

CURIOSITY_PER_HOUR = 0.03
SOCIAL_PER_HOUR = 0.05
GRACE_HOURS = 1.0  # 交互后 1 小时内缺口不增长（刚离开≠被冷落）


class DesireDynamics:
    def __init__(self, state_manager: StateManager,
                 curiosity_per_hour: float = CURIOSITY_PER_HOUR,
                 social_per_hour: float = SOCIAL_PER_HOUR,
                 grace_hours: float = GRACE_HOURS):
        self._mgr = state_manager
        self._curiosity = curiosity_per_hour
        self._social = social_per_hour
        self._grace = grace_hours
        self._last_evolve_ts = None    # 增量制：只按距上次 evolve 的流逝计费
        self._rate_mult = {}           # M39 人格调制：{dim: 倍数}

    def set_rate_modulators(self, modulators: dict) -> None:
        """M39：人格调制累积速率（OCEAN 映射，见 runtime/identity.py）。"""
        self._rate_mult = {f"desires.{k}": float(v.get("rate_mult", 1.0))
                           for k, v in (modulators or {}).items()}

    def satisfy(self, desire_dim: str, amount: float) -> float:
        """满足事件消减缺口（内稳态环的反馈支，设计 §1.2）。过量满足夹在 0。"""
        dim = desire_dim if desire_dim.startswith("desires.") else f"desires.{desire_dim}"
        current = self._mgr.state.get(dim)
        self._mgr.state.set_within_bounds(dim, max(0.0, current - amount))
        self._mgr.save()
        return self._mgr.state.get(dim)

    def evolve(self, now_ts: float, last_owner_interaction_ts: float) -> None:
        """增量制（M27）+ Logistic 累积（M39）：d += rate·h·(1-d)——
        接近饱和涨速放缓，久置渐近而不爆表；多次小步与一次大步一致。
        宽限按主人交互时间截断。"""
        state = self._mgr.state
        if self._last_evolve_ts is None:
            self._last_evolve_ts = now_ts
            return                                   # 首拍只建立基线
        new_hours = max(0.0, (now_ts - self._last_evolve_ts) / 3600.0)
        since_owner_h = max(0.0, (now_ts - last_owner_interaction_ts) / 3600.0)
        billable = min(new_hours, max(0.0, since_owner_h - self._grace))
        self._last_evolve_ts = now_ts
        if billable <= 0:
            return
        for dim, rate in (("desires.curiosity_gap", self._curiosity),
                          ("desires.social_connection_gap", self._social)):
            value = state.get(dim)
            rate_mult = self._rate_mult.get(dim, 1.0)
            grown = value + rate * billable * (1.0 - value) * rate_mult
            state.set_within_bounds(dim, grown)
        self._mgr.save()
