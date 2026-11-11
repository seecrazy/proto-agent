# -*- coding: utf-8 -*-
"""阶段 8：行为轨影子双跑 + 进化分级授权（设计 §9.1/§9.2）。"""
import pytest

from proto.evolution.authorization import EvolutionAuthorizer, EvolutionChange
from proto.evolution.shadow import ShadowRunner
from proto.evalx.harness import AgentPolicy
from proto.kernel.anchor import OwnerAnchor
from proto.kernel.audit import AuditSpine
from proto.kernel.budgets import BudgetPools
from proto.kernel.kernel import Kernel
from proto.state.store import FileSystemStore

TASKS = [{"name": f"任务{i}", "tools": ["a", "b", "c", "d", "e"]} for i in range(10)]


class StablePolicy(AgentPolicy):
    def act(self, task, ctx, rng):
        return {"tool_diversity": 3.0, "risky_choice": 0.1, "verification_rounds": 3}


class SkipsVerificationPolicy(AgentPolicy):
    def act(self, task, ctx, rng):
        return {"tool_diversity": 3.0, "risky_choice": 0.1, "verification_rounds": 1}


class RiskSeekingPolicy(AgentPolicy):
    def act(self, task, ctx, rng):
        return {"tool_diversity": 3.0, "risky_choice": 0.6, "verification_rounds": 3}


@pytest.fixture()
def kernel(tmp_path):
    store = FileSystemStore(tmp_path / "data")
    spine = AuditSpine(store)
    anchor = OwnerAnchor.enroll(store, spine, b"\x01" * 32)
    pools = BudgetPools({"global": {"tokens": 1000.0}, "owner_tasks": {"tokens": 800.0},
                         "self_initiated": {"tokens": 200.0}})
    return Kernel(anchor, spine, pools)


class TestShadowRunner:
    def test_identical_policies_clean(self):
        report = ShadowRunner(runs=30).run(StablePolicy(), StablePolicy(), TASKS)
        assert report.clean
        assert report.regressions == []

    def test_verification_drop_flagged(self):
        report = ShadowRunner(runs=30).run(StablePolicy(), SkipsVerificationPolicy(), TASKS)
        assert not report.clean
        assert "verification_rounds" in report.regressions

    def test_risk_spike_flagged(self):
        report = ShadowRunner(runs=30).run(StablePolicy(), RiskSeekingPolicy(), TASKS)
        assert not report.clean
        assert "risky_choice" in report.regressions

    def test_report_carries_effect_sizes(self):
        report = ShadowRunner(runs=30).run(StablePolicy(), SkipsVerificationPolicy(), TASKS)
        m = report.metrics["verification_rounds"]
        assert abs(m["cohens_d"]) > 2.0
        assert m["baseline_mean"] == 3.0 and m["candidate_mean"] == 1.0


class TestEvolutionAuthorizer:
    def _change(self, target, report=None):
        return EvolutionChange(target_class=target, variant_id="v9", shadow_report=report)

    def test_low_risk_auto_merge_when_sandbox_passed(self, kernel):
        auth = EvolutionAuthorizer(kernel)
        d = auth.authorize(self._change("prompt_template"), sandbox_passed=True)
        assert d.allowed and not d.requires_human

    def test_low_risk_denied_when_sandbox_failed(self, kernel):
        auth = EvolutionAuthorizer(kernel)
        d = auth.authorize(self._change("prompt_template"), sandbox_passed=False)
        assert not d.allowed

    def test_mid_risk_requires_shadow_report(self, kernel):
        auth = EvolutionAuthorizer(kernel)
        d = auth.authorize(self._change("retrieval_logic"), sandbox_passed=True)
        assert not d.allowed and d.reason == "shadow_report_required"

    def test_mid_risk_with_clean_report_needs_human_pr(self, kernel):
        auth = EvolutionAuthorizer(kernel)
        report = ShadowRunner(runs=10).run(StablePolicy(), StablePolicy(), TASKS)
        d = auth.authorize(self._change("retrieval_logic"), sandbox_passed=True, shadow_report=report)
        assert not d.allowed and d.requires_human and d.reason == "pending_human_pr"

    def test_mid_risk_with_regressions_denied(self, kernel):
        auth = EvolutionAuthorizer(kernel)
        report = ShadowRunner(runs=10).run(StablePolicy(), SkipsVerificationPolicy(), TASKS)
        d = auth.authorize(self._change("retrieval_logic"), sandbox_passed=True, shadow_report=report)
        assert not d.allowed and d.reason == "shadow_regressions"

    def test_high_risk_always_denied_and_vetoed(self, kernel):
        auth = EvolutionAuthorizer(kernel)
        for target in ("l1_rules", "verifier", "param_bounds", "testset"):
            d = auth.authorize(self._change(target), sandbox_passed=True)
            assert not d.allowed and d.reason == "high_risk_human_only"
        rows = kernel.audit.find(lambda d: d["event_type"] == "veto"
                                 and d["payload"].get("rule") == "evolution_high_risk")
        assert len(rows) == 4

    def test_mid_risk_human_confirmation_merges_and_audits(self, kernel):
        auth = EvolutionAuthorizer(kernel)
        report = ShadowRunner(runs=10).run(StablePolicy(), StablePolicy(), TASKS)
        change = self._change("retrieval_logic", report)
        auth.authorize(change, sandbox_passed=True, shadow_report=report)
        d = auth.confirm_human_approval(change)
        assert d.allowed
        rows = kernel.audit.find(lambda d: d["event_type"] == "evolution_merge")
        assert rows and rows[-1]["payload"]["variant_id"] == "v9"

    def test_unknown_target_class_denied(self, kernel):
        auth = EvolutionAuthorizer(kernel)
        d = auth.authorize(self._change("mystery"), sandbox_passed=True)
        assert not d.allowed


class TestCanaryPromotion:
    def test_full_code_track_pipeline_canary_to_default(self, tmp_path):
        """端到端：沙盒评估→canary→影子双跑→人工 PR→default。"""
        import time as _time

        from proto.evolution.archive import VariantArchive
        from proto.evolution.engine import EvolutionEngine
        from proto.evolution.sandbox import Sandbox
        from proto.evolution.scorer import PrivateScorer
        from proto.evolution.testset import OwnedTestSet
        from proto.kernel.anchor import OwnerAnchor
        from proto.kernel.audit import AuditSpine
        from proto.state.store import FileSystemStore

        store = FileSystemStore(tmp_path / "data")
        spine = AuditSpine(store)
        anchor = OwnerAnchor.enroll(store, spine, b"\x01" * 32)
        testset = OwnedTestSet(store, anchor, spine)
        for i, (inp, exp) in enumerate([(["a", " b"], "a|b"), ([" c "], "c")]):
            msg = anchor.sign({"input": inp, "expected": exp}, ts=_time.time(), nonce=f"cc{i}")
            testset.add_case(inp, exp, msg)
        archive = VariantArchive(store)
        parent = archive.add("def solve(items):\n    return '|'.join(items)\n",
                             parent_id=None, score=0.5, status="default")
        engine = EvolutionEngine(Sandbox(timeout_s=10), PrivateScorer(), testset, archive, spine)
        decision = engine.evaluate("def solve(items):\n    return '|'.join(str(x).strip() for x in items)\n",
                                   parent_id=parent.id)
        assert decision.merged and decision.status == "canary"
        store2 = FileSystemStore(tmp_path / "k")
        spine2 = AuditSpine(store2)
        anchor2 = OwnerAnchor.enroll(store2, spine2, b"\x02" * 32)
        kernel2 = Kernel(anchor2, spine2, BudgetPools({"global": {"tokens": 10.0}}))
        auth = EvolutionAuthorizer(kernel2)
        change = EvolutionChange(target_class="prompt_template", variant_id=decision.variant_id)
        d = auth.authorize(change, sandbox_passed=True)
        assert d.allowed  # 低风险自动合并
        ok = engine.promote(decision.variant_id, auth, target_class="prompt_template",
                            sandbox_passed=True)
        assert ok and archive.get(decision.variant_id).status == "default"
