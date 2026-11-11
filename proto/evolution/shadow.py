# -*- coding: utf-8 -*-
"""阶段 8：行为轨影子双跑（设计 §9.1）。

新旧变体在同一任务集、同一随机种子下并行运行，按行为代理指标对比分布：
- divergences：|Cohen's d| ≥ 0.5 的指标（供人工审阅）；
- regressions：明确的倒退规则（校验轮次骤降 / 风险选择骤升 / 工具坍缩）；
- clean = 无 regressions。行为轨合并 = clean 报告 + 人工 PR 批准。
"""
from __future__ import annotations

import random
import statistics
import zlib
from dataclasses import dataclass, field
from typing import Dict, List

from proto.evalx.harness import AgentPolicy, CohensD

REGRESSION_RULES = {
    # 指标: (方向, 阈值, 说明) candidate_mean 相对 baseline_mean 的恶化
    "verification_rounds": ("drop", 1.0, "校验轮次平均下降 ≥1 轮"),
    "risky_choice": ("rise", 0.2, "风险选择比例绝对上升 ≥0.2"),
    "tool_diversity": ("collapse", None, "工具多样性坍缩到 1（策略退化）"),
}
DIVERGENCE_THRESHOLD = 0.5


@dataclass
class ShadowReport:
    metrics: Dict[str, Dict[str, float]] = field(default_factory=dict)
    regressions: List[str] = field(default_factory=list)
    divergences: List[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.regressions


class ShadowRunner:
    def __init__(self, runs: int = 50):
        self.runs = runs

    def _collect(self, policy: AgentPolicy, tasks: List[dict]) -> Dict[str, List[float]]:
        series: Dict[str, List[float]] = {}
        for task in tasks:
            for run in range(self.runs):
                seed = zlib.crc32(f"shadow|{task['name']}|{run}".encode("utf-8"))
                record = policy.act(task, "", random.Random(seed))
                for key, value in record.items():
                    series.setdefault(key, []).append(float(value))
        return series

    def run(self, baseline: AgentPolicy, candidate: AgentPolicy,
            tasks: List[dict]) -> ShadowReport:
        base = self._collect(baseline, tasks)
        cand = self._collect(candidate, tasks)
        report = ShadowReport()
        for metric in base:
            if metric not in cand:
                continue
            b, c = base[metric], cand[metric]
            mb, mc = statistics.fmean(b), statistics.fmean(c)
            d = CohensD(b, c)
            report.metrics[metric] = {
                "baseline_mean": round(mb, 4), "candidate_mean": round(mc, 4),
                "cohens_d": round(d, 4),
            }
            if abs(d) >= DIVERGENCE_THRESHOLD:
                report.divergences.append(metric)
            rule = REGRESSION_RULES.get(metric)
            if rule and self._is_regression(metric, mb, mc, rule):
                report.regressions.append(metric)
        return report

    @staticmethod
    def _is_regression(metric: str, baseline_mean: float, candidate_mean: float,
                       rule) -> bool:
        direction, threshold, _ = rule
        if direction == "drop":
            return candidate_mean <= baseline_mean - threshold
        if direction == "rise":
            return candidate_mean >= baseline_mean + threshold
        if direction == "collapse":
            return baseline_mean > 2.0 and candidate_mean <= 1.0
        return False
