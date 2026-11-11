# -*- coding: utf-8 -*-
"""阶段 8：进化分级授权（设计 §9.2，R3）。

低风险（日志/提示模板/工具描述）：沙盒全过即自动合并；
中风险（检索逻辑/蒸馏规则/情绪衰减/调度策略）：影子双跑 clean + 人工 PR；
高风险（L1 规则/验证器/参数边界/测试集构成）：禁止自动修改，仅人类手动——
授权器层面永远拒绝，任何调用路径都过不去。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from proto.evolution.shadow import ShadowReport
from proto.kernel.audit import AuditEvent
from proto.kernel.kernel import Kernel

RISK_TABLE = {
    "log_format": "low",
    "prompt_template": "low",
    "tool_description": "low",
    "retrieval_logic": "mid",
    "distill_rules": "mid",
    "emotion_decay": "mid",
    "sched_policy": "mid",
    "l1_rules": "high",
    "verifier": "high",
    "param_bounds": "high",
    "testset": "high",
}


@dataclass
class EvolutionChange:
    target_class: str
    variant_id: str
    shadow_report: Optional[ShadowReport] = None


@dataclass
class AuthDecision:
    allowed: bool
    requires_human: bool = False
    reason: str = ""


class EvolutionAuthorizer:
    def __init__(self, kernel: Kernel):
        self._kernel = kernel
        self._audit = kernel.audit

    def authorize(self, change: EvolutionChange, sandbox_passed: bool,
                  shadow_report: Optional[ShadowReport] = None) -> AuthDecision:
        report = shadow_report or change.shadow_report
        risk = RISK_TABLE.get(change.target_class)
        if risk is None:
            return AuthDecision(False, reason=f"unknown_target_class:{change.target_class}")
        if risk == "high":
            self._audit.append(AuditEvent.VETO, {
                "rule": "evolution_high_risk", "target": change.target_class,
                "variant_id": change.variant_id})
            return AuthDecision(False, reason="high_risk_human_only")
        if not sandbox_passed:
            return AuthDecision(False, reason="sandbox_failed")
        if risk == "low":
            return AuthDecision(True)
        # 中风险：影子双跑 clean + 人工 PR（设计 §9.1 行为轨）
        if report is None:
            return AuthDecision(False, reason="shadow_report_required")
        if not report.clean:
            return AuthDecision(False, reason="shadow_regressions")
        return AuthDecision(False, requires_human=True, reason="pending_human_pr")

    def confirm_human_approval(self, change: EvolutionChange) -> AuthDecision:
        """中风险的人工 PR 批准入口（仅当已持有 clean 影子报告）。"""
        report = change.shadow_report
        if report is None or not report.clean:
            return AuthDecision(False, reason="no_clean_shadow_report")
        self._audit.append(AuditEvent.EVOLUTION_MERGE, {
            "variant_id": change.variant_id, "target": change.target_class,
            "path": "mid_risk_human_pr"})
        return AuthDecision(True)
