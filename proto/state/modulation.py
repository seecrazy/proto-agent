# -*- coding: utf-8 -*-
"""L2 分池归因累积与调制管线（设计 §3.1 / §7.1，R1 防毒化核心）。

三通道各自独立累积器；CONTENT 池上限 = threshold × content_cap_ratio（默认 0.5），
数学上无论洪泛多少次都无法单独触发调制；触顶时写一次 SATURATION_ALERT 审计。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from proto.kernel.anchor import Channel, SOURCE_WEIGHT
from proto.kernel.audit import AuditSpine
from proto.state.agent_state import ModulationResult, StateManager


@dataclass
class AttributionSignal:
    param: str
    direction: float   # +1 / -1
    strength: float    # (0, 1]
    channel: Channel


@dataclass
class PendingModulation:
    param: str
    delta: float
    channel: Channel
    accumulated: float = 0.0


class AttributionAccumulator:
    def __init__(self, audit: AuditSpine, threshold: float = 0.30, step: float = 0.02,
                 content_cap_ratio: float = 0.5):
        # audit 必传：饱和告警是主人依赖的产品信号，不允许静默丢失（M8）
        self.threshold = threshold
        self.step = step
        self.content_cap = threshold * content_cap_ratio
        self._audit = audit
        # (param, channel) -> 加权累积量
        self._pools: Dict[tuple, float] = {}
        self._saturated: Dict[str, bool] = {}

    def add(self, param: str, direction: float, strength: float, channel: Channel) -> None:
        # 输入域在信任边界处强制（设计 §7.1：LLM 误判无法放大权限）
        direction = 1.0 if direction >= 0 else -1.0
        strength = max(0.0, min(1.0, strength))
        key = (param, channel)
        weight = SOURCE_WEIGHT[channel]
        self._pools[key] = self._pools.get(key, 0.0) + direction * strength * weight
        if channel is Channel.CONTENT and self._pools[key] >= self.content_cap:
            self._pools[key] = self.content_cap
            if not self._saturated.get(param):
                self._audit.append("saturation_alert", {
                    "param": param, "cap": self.content_cap,
                    "note": "CONTENT 通道累积触顶，已按设计 §3.1 饱和",
                })
            self._saturated[param] = True

    def ready(self) -> List[PendingModulation]:
        pending = []
        for key in sorted(self._pools):
            param, channel = key
            if channel is Channel.CONTENT:
                continue  # 数学上不可达阈值，双保险
            total = self._pools[key]
            if abs(total) >= self.threshold:
                pending.append(PendingModulation(
                    param=param,
                    delta=(1 if total > 0 else -1) * self.step,
                    channel=channel,
                    accumulated=round(total, 4),
                ))
                # 只清空已出队的池；未达阈值的累积跨批次保留（保守归因语义）
                del self._pools[key]
        return pending

    def pool_level(self, param: str, channel: Channel) -> float:
        return self._pools.get((param, channel), 0.0)


class ModulationPipeline:
    """归因信号 → 累积 → 达阈值 → StateManager 有界调制。"""

    def __init__(self, accumulator: AttributionAccumulator, state_manager: StateManager):
        self._acc = accumulator
        self._mgr = state_manager

    def process(self, signals: List[AttributionSignal]) -> List[ModulationResult]:
        for s in signals:
            self._acc.add(s.param, s.direction, s.strength, s.channel)
        results = []
        for pending in self._acc.ready():
            results.append(self._mgr.apply_modulation(
                pending.param, pending.delta, pending.channel.value,
                accumulated=pending.accumulated))
        return results
