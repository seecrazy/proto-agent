# -*- coding: utf-8 -*-
"""L3 反馈归因器（设计 §7.1）。

Attributor 为可插拔接口：HeuristicAttributor 离线关键词规则；
LLMAttributor 后续接模型 API。两者输出统一 AttributionSignal，
通道权重与累积池逻辑在归因器之外——LLM 误判无法放大权限。
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Dict, List, Tuple

from proto.kernel.anchor import Channel
from proto.state.modulation import AttributionSignal

# (正则, 参数, 方向, 强度)
_RULES: List[Tuple[str, str, float, float]] = [
    (r"更主动|主动一点|主动些|不用事事问", "instincts.social_initiative", +1, 0.5),
    (r"太主动|别自作主张|先问我", "instincts.social_initiative", -1, 0.5),
    (r"太冒险|激进|稳一点|保守些", "instincts.risk_tolerance", -1, 0.5),
    (r"大胆一点|可以冒险|激进一点", "instincts.risk_tolerance", +1, 0.4),
    (r"多探索|多尝试|试试新", "instincts.exploration", +1, 0.4),
    (r"别折腾|收敛一点|专注", "instincts.exploration", -1, 0.4),
    (r"更严谨|更仔细|认真点", "personality.rigor", +1, 0.5),
    (r"别纠结|果断一点|直接做", "personality.rigor", -1, 0.4),
    (r"直接说|直白一点|别绕", "personality.directness", +1, 0.4),
    (r"委婉一点|客气些", "personality.directness", -1, 0.4),
    (r"自己判断|独立思考|你自己决定", "personality.independence", +1, 0.5),
    (r"多请示|多汇报|及时同步", "personality.independence", -1, 0.4),
    (r"完美|打磨|再润色", "instincts.perfectionism", +1, 0.4),
    (r"先出结果|能跑就行|快速交付", "instincts.perfectionism", -1, 0.4),
]


class Attributor(ABC):
    @abstractmethod
    def attribute(self, text: str, channel: Channel) -> List[AttributionSignal]:
        """从一段反馈文本中归因出参数调制信号。"""


class HeuristicAttributor(Attributor):
    def attribute(self, text: str, channel: Channel) -> List[AttributionSignal]:
        signals = []
        for pattern, param, direction, strength in _RULES:
            if re.search(pattern, text):
                signals.append(AttributionSignal(
                    param=param, direction=direction, strength=strength, channel=channel))
        return signals

    @staticmethod
    def rule_table() -> List[Dict[str, object]]:
        return [{"pattern": p, "param": param, "direction": d, "strength": s}
                for p, param, d, s in _RULES]
