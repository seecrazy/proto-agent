# -*- coding: utf-8 -*-
"""Task 9a: 行为评估 harness（设计 §6.2 / 落地阶段 1 验证方式）。

同一任务在不同状态快照下的行为分布差异（效应量 ≥ 0.5）。
"""
import random

import pytest

from proto.evalx.harness import AgentPolicy, CohensD, EvalHarness, ParametricPolicy
from proto.state.agent_state import AgentState


def snapshot(exploration, arousal=0.4):
    s = AgentState()
    s.instincts["exploration"]["value"] = exploration
    s.emotion["arousal"] = arousal
    return s


class TestCohensD:
    def test_identical_samples_zero(self):
        assert CohensD([1.0] * 5, [1.0] * 5) == 0.0

    def test_disjoint_samples_large(self):
        d = CohensD([0.0] * 10, [10.0] * 10)
        assert abs(d) > 5.0


class TestParametricPolicy:
    def test_epsilon_drives_tool_diversity(self):
        low = ParametricPolicy()
        high = ParametricPolicy()
        tasks = [{"name": f"任务{i}", "tools": ["搜索", "读文件", "执行命令", "网络请求", "数据库"]} for i in range(30)]
        harness = EvalHarness(runs_per_cell=40)
        report = harness.run(
            policy_cls=ParametricPolicy,
            tasks=tasks,
            snapshots={"low": snapshot(0.15, 0.1), "high": snapshot(0.85, 0.95)},
        )
        d = report.effect("low", "high", "tool_diversity")
        assert abs(d) >= 0.5, report.metrics_summary("tool_diversity")


class TestEvalHarnessReport:
    def test_report_lists_measurable_proxies(self):
        harness = EvalHarness(runs_per_cell=30)
        tasks = [{"name": f"任务{i}", "tools": ["a", "b", "c", "d", "e"]} for i in range(20)]
        report = harness.run(
            policy_cls=ParametricPolicy,
            tasks=tasks,
            snapshots={"low": snapshot(0.15, 0.1), "high": snapshot(0.85, 0.95)},
        )
        pairs = report.measurable(metric="tool_diversity", threshold=0.5)
        assert pairs, report.metrics_summary("tool_diversity")
