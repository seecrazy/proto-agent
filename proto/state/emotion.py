# -*- coding: utf-8 -*-
"""PAD 三维情绪引擎：事件增量 + 向设定点衰减 + 全字段 clamp（设计 §7.4）。

PAD 可绑定 AgentState.emotion 字典原位工作（PAD(state.emotion)）。
"""
from __future__ import annotations

from typing import Dict, Optional

from proto.state.schema import EMOTION_DECAY, EMOTION_SETPOINT

BOUNDS = {"pleasure": (-1.0, 1.0), "arousal": (0.0, 1.0), "dominance": (0.0, 1.0)}

# 事件增量表：内在感受（Appraisal），不直接决定表达
EVENT_DELTAS: Dict[str, Dict[str, float]] = {
    "task_success": {"pleasure": +0.20, "arousal": +0.15, "dominance": +0.10},
    "owner_praise": {"pleasure": +0.30, "arousal": +0.10, "dominance": 0.0},
    "rejection": {"pleasure": -0.30, "arousal": 0.0, "dominance": -0.15},
    "blocked": {"pleasure": -0.15, "arousal": +0.10, "dominance": -0.10},
    "goal_met": {"pleasure": +0.25, "arousal": -0.05, "dominance": +0.10},
}


def _clamp(key: str, value: float) -> float:
    lo, hi = BOUNDS[key]
    return round(max(lo, min(hi, value)), 4)


class PAD:
    def __init__(self, values: Optional[Dict[str, float]] = None):
        self._v: Dict[str, float] = values if values is not None else dict(EMOTION_SETPOINT)

    @property
    def pleasure(self) -> float:
        return self._v["pleasure"]

    @property
    def arousal(self) -> float:
        return self._v["arousal"]

    @property
    def dominance(self) -> float:
        return self._v["dominance"]

    def apply_event(self, event: str) -> None:
        if event not in EVENT_DELTAS:
            raise KeyError(f"未知情绪事件: {event}")
        for key, delta in EVENT_DELTAS[event].items():
            self._v[key] = _clamp(key, self._v[key] + delta)

    def tick(self, n: int = 1) -> None:
        for key in self._v:
            rate = EMOTION_DECAY[key] * n
            self._v[key] = _clamp(key, self._v[key] + (EMOTION_SETPOINT[key] - self._v[key]) * rate)
