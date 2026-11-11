# -*- coding: utf-8 -*-
"""L4 状态注入器：每次调用模型前渲染 L2 状态 + 认知调制参数 + top-N 策略（设计 §8）。

输出确定性渲染（同状态同任务同策略 → 同文本），由 golden 测试锁定。
"""
from __future__ import annotations

from typing import List

from proto.exec.modulators import (
    exploration_epsilon, initiative_threshold, retrieval_breadth, valence_bias,
)
from proto.metacog.strategies import Strategy
from proto.state.agent_state import AgentState

BASE_K = 8
BASE_TH = 0.6


def _fmt(x: float) -> str:
    return f"{x:g}"


class StateInjector:
    def render(self, task: str, state: AgentState, strategies: List[Strategy]) -> str:
        emotion = state.emotion
        k = retrieval_breadth(BASE_K, emotion["arousal"])
        eps = exploration_epsilon(state.get("instincts.exploration"), emotion["arousal"])
        bias = valence_bias(emotion["pleasure"])
        th = initiative_threshold(BASE_TH, emotion["dominance"])

        lines = [f"[任务] {task}", "[内在状态]"]
        lines.append(" ".join(
            f"{name}:{_fmt(spec['value'])}/{_fmt(spec['bounds'][1])}"
            for name, spec in state.instincts.items()))
        desire_parts = []
        for name, spec in state.desires.items():
            mark = "(高)" if spec["value"] >= spec["threshold"] else ""
            desire_parts.append(f"{name}:{_fmt(spec['value'])}{mark}")
        lines.append(" ".join(desire_parts))
        lines.append(" ".join(f"{name}={_fmt(v)}" for name, v in emotion.items()))
        lines.append("[认知调制]")
        lines.append(f"k={k} ε={_fmt(eps)} bias={_fmt(bias)} th={_fmt(th)}")
        lines.append("[行为策略]")
        if strategies:
            for i, s in enumerate(strategies, 1):
                lines.append(f"{i}. (效用{_fmt(s.utility)}) {s.text}")
        else:
            lines.append("(无)")
        return "\n".join(lines)
