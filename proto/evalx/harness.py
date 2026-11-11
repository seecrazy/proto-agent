# -*- coding: utf-8 -*-
"""行为评估 harness（设计 §6.2，落地阶段 1 的验证方式）。

同一任务 × 不同状态快照 → 注入上下文 → 策略产生行为 → 聚合行为代理指标 →
效应量（Cohen's d）判定参数是否"测得出行为差异"（|d| ≥ 0.5）。

诚实边界：ParametricPolicy 是参数驱动的仿真策略，验证的是调制链路本身；
LLM 策略适配器接入后，同一 harness 直接测量真实模型行为分布。
"""
from __future__ import annotations

import re
import statistics
import zlib
from abc import ABC, abstractmethod
from random import Random
from typing import Dict, List, Type

from proto.exec.injector import StateInjector
from proto.state.agent_state import AgentState

_EPS = re.compile(r"ε=([0-9.]+)")
_K = re.compile(r"k=(\d+)")


def CohensD(a: List[float], b: List[float]) -> float:
    """效应量；两常量样本均值为 0，不相等则给极大有限值。"""
    ma, mb = statistics.fmean(a), statistics.fmean(b)
    va = statistics.pvariance(a) if len(a) > 1 else 0.0
    vb = statistics.pvariance(b) if len(b) > 1 else 0.0
    pooled = ((va + vb) / 2.0) ** 0.5
    if pooled < 1e-12:
        return 0.0 if abs(ma - mb) < 1e-12 else (ma - mb) / 1e-12
    return (ma - mb) / pooled


class AgentPolicy(ABC):
    """策略接口：LLM 适配器实现同接口即可被同一 harness 测量。"""

    @abstractmethod
    def act(self, task: dict, injected_context: str, rng: Random) -> dict:
        """执行一次任务，返回行为记录。"""


class ParametricPolicy(AgentPolicy):
    """按注入上下文中的 ε/k 驱动的仿真策略：以 ε 概率选非常规工具/高风险选项。"""

    def act(self, task: dict, injected_context: str, rng: Random) -> dict:
        m_eps = _EPS.search(injected_context)
        m_k = _K.search(injected_context)
        eps = float(m_eps.group(1)) if m_eps else 0.05
        k = int(m_k.group(1)) if m_k else 8
        tools = task["tools"]
        used = []
        for _ in range(k):
            if rng.random() < eps:
                used.append(rng.choice(tools))
            else:
                used.append(tools[0])
        risky = rng.random() < eps
        return {"tools_used": used, "risky_choice": 1.0 if risky else 0.0}


class EvalReport:
    def __init__(self, data: Dict[str, Dict[str, List[float]]]):
        self._data = data

    def values(self, snapshot: str, metric: str) -> List[float]:
        return self._data[snapshot][metric]

    def effect(self, a: str, b: str, metric: str) -> float:
        return CohensD(self.values(a, metric), self.values(b, metric))

    def measurable(self, metric: str, threshold: float = 0.5) -> List[str]:
        names = sorted(self._data)
        pairs = []
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                if abs(self.effect(a, b, metric)) >= threshold:
                    pairs.append(f"{a}|{b}")
        return pairs

    def metrics_summary(self, metric: str) -> Dict[str, float]:
        return {name: round(statistics.fmean(vals[metric]), 3)
                for name, vals in self._data.items()}


class EvalHarness:
    def __init__(self, runs_per_cell: int = 50):
        self.runs = runs_per_cell
        self._injector = StateInjector()

    def run(self, policy_cls: Type[AgentPolicy], tasks: List[dict],
            snapshots: Dict[str, AgentState]) -> EvalReport:
        data: Dict[str, Dict[str, List[float]]] = {}
        for snap_name, state in snapshots.items():
            data[snap_name] = {"tool_diversity": [], "risky_choice": []}
            for task in tasks:
                context = self._injector.render(task["name"], state, [])
                for run in range(self.runs):
                    seed = zlib.crc32(f"{snap_name}|{task['name']}|{run}".encode("utf-8"))
                    policy = policy_cls()
                    record = policy.act(task, context, Random(seed))
                    data[snap_name]["tool_diversity"].append(
                        float(len(set(record["tools_used"]))))
                    data[snap_name]["risky_choice"].append(record["risky_choice"])
        return EvalReport(data)
