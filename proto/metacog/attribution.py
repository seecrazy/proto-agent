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

import json
import re

from proto.kernel.anchor import Channel
from proto.state.modulation import AttributionSignal

# (正则, 参数, 方向, 强度)
_RULES: List[Tuple[str, str, float, float]] = [
    (r"更主动|主动一点|主动些|不用事事问", "instincts.social_initiative", +1, 0.5),
    (r"太主动|别自作主张|先问我", "instincts.social_initiative", -1, 0.5),
    (r"太冒险|太激进|别激进|激进过了|稳一点|保守些", "instincts.risk_tolerance", -1, 0.5),
    (r"大胆一点|可以冒险|激进一点|冒点险", "instincts.risk_tolerance", +1, 0.4),
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


def extract_json_array(text: str) -> list:
    """从模型输出中稳健提取 JSON 数组（容忍 ```json 围栏与前后缀文本）。"""
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("输出中没有 JSON 数组")
    return json.loads(cleaned[start:end + 1])


class LLMAttributor(Attributor):
    """LLM 归因：输出统一 AttributionSignal，参数白名单 + 输入域夹紧，
    任何解析失败回退到启发式归因器——LLM 误判无法放大权限（设计 §7.1）。"""

    PARAM_WHITELIST = frozenset({
        "instincts.exploration", "instincts.risk_tolerance",
        "instincts.social_initiative", "instincts.perfectionism",
        "desires.curiosity_gap", "desires.goal_urgency",
        "desires.social_connection_gap", "desires.recognition_need",
        "personality.rigor", "personality.proactiveness",
        "personality.directness", "personality.independence",
    })

    _PROMPT = (
        "你是成长型 Agent PROTO 的反馈归因器。根据主人反馈判断它指向哪个内在参数、"
        "方向与强度。参数只能从以下选择：\n" + "\n".join(sorted(PARAM_WHITELIST)) +
        "\n方向：+1 表示调高倾向，-1 表示调低。强度 0~1。\n"
        '只输出 JSON 数组，例如 [{"param": "instincts.social_initiative", '
        '"direction": 1, "strength": 0.6}]。没有明确指向就输出 []。'
    )

    def __init__(self, client, fallback: HeuristicAttributor = None):
        self._client = client
        self._fallback = fallback or HeuristicAttributor()

    def attribute(self, text: str, channel: Channel) -> List[AttributionSignal]:
        try:
            rows = extract_json_array(self._client.complete(self._PROMPT, text))
        except Exception:
            return self._fallback.attribute(text, channel)
        signals = []
        for row in rows:
            param = row.get("param")
            if param not in self.PARAM_WHITELIST:
                continue
            direction = 1 if float(row.get("direction", 0)) >= 0 else -1
            strength = max(0.0, min(1.0, float(row.get("strength", 0.5))))
            signals.append(AttributionSignal(
                param=param, direction=direction, strength=strength, channel=channel))
        return signals
