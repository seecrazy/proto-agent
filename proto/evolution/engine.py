# -*- coding: utf-8 -*-
"""阶段 7：代码轨进化引擎（设计 §9.1 / §9.3）。

闭环：采样变体 → 沙盒跑人类测试集 → 私有评分 →
（全用例通过 且 评分 ≥ 父代）→ 入档 canary → EVOLUTION_MERGE 审计。
canary → default 的晋升属中风险，走阶段 8 影子双跑 + 分级授权。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from proto.evolution.archive import VariantArchive
from proto.evolution.sandbox import Sandbox, SandboxResult
from proto.evolution.scorer import PrivateScorer
from proto.evolution.testset import OwnedTestSet
from proto.kernel.audit import AuditEvent, AuditSpine


@dataclass
class EngineDecision:
    merged: bool
    status: str          # canary | rejected
    score: float
    reason: str = ""
    variant_id: Optional[str] = None
    sandbox_result: Optional[SandboxResult] = None


class EvolutionEngine:
    def __init__(self, sandbox: Sandbox, scorer: PrivateScorer,
                 testset: OwnedTestSet, archive: VariantArchive, audit: AuditSpine):
        self._sandbox = sandbox
        self._scorer = scorer
        self._testset = testset
        self._archive = archive
        self._audit = audit

    def evaluate(self, code: str, parent_id: Optional[str]) -> EngineDecision:
        cases = self._testset.cases()
        parent = self._archive.get(parent_id) if parent_id else None
        parent_score = parent.score if parent else 0.0
        result = self._sandbox.run_variant(code, cases)
        score = self._scorer.score(result)
        # 门 1：分数不得退步（含满分原地踏步）——退步变体直接出局
        if score < parent_score or (parent is not None and score == parent_score and parent_score == 1.0):
            variant = self._archive.add(code, parent_id, score, "rejected")
            return EngineDecision(False, "rejected", score, reason="score_not_improved",
                                  variant_id=variant.id, sandbox_result=result)
        # 门 2：人类测试集必须全过——满分含义由全集定义（设计 §9.1）
        if result.passed < result.total:
            variant = self._archive.add(code, parent_id, score, "rejected")
            return EngineDecision(False, "rejected", score, reason="cases_failed",
                                  variant_id=variant.id, sandbox_result=result)
        variant = self._archive.add(code, parent_id, score, "canary")
        self._audit.append(AuditEvent.EVOLUTION_MERGE, {
            "variant_id": variant.id, "parent_id": parent_id,
            "score": score, "status": "canary",
        })
        return EngineDecision(True, "canary", score,
                              variant_id=variant.id, sandbox_result=result)
