# -*- coding: utf-8 -*-
"""阶段 7：测试集治理（人类所有/只增不删）+ 沙盒隔离 + 私有评分 + 档案库 + 进化引擎（设计 §9）。"""
import time

import pytest

from proto.evolution.archive import VariantArchive
from proto.evolution.engine import EvolutionEngine
from proto.evolution.sandbox import Sandbox
from proto.evolution.scorer import PrivateScorer
from proto.evolution.testset import OwnedTestSet
from proto.kernel.anchor import OwnerAnchor
from proto.kernel.audit import AuditSpine
from proto.state.store import FileSystemStore

BASELINE = "def solve(items):\n    return '|'.join(items)\n"
IMPROVED = "def solve(items):\n    return '|'.join(str(x).strip() for x in items)\n"
BROKEN = "def solve(items):\n    raise RuntimeError('boom')\n"
SLOW = "import time\ndef solve(items):\n    time.sleep(30)\n    return '|'.join(items)\n"

CASES = [
    (["a", "b"], "a|b"),          # 基线也能过
    ([" a ", "b"], "a|b"),        # 需要裁剪——基线过不了
    (["x  y", " z "], "x  y|z"),
    (["a", "b", "c"], "a|b|c"),   # 3 元素——partial 变体过不了
]


@pytest.fixture()
def env(tmp_path):
    store = FileSystemStore(tmp_path / "data")
    spine = AuditSpine(store)
    anchor = OwnerAnchor.enroll(store, spine, b"\x01" * 32)
    testset = OwnedTestSet(store, anchor, spine)
    for i, (inp, exp) in enumerate(CASES):
        msg = anchor.sign({"case": i, "input": inp, "expected": exp},
                          ts=time.time(), nonce=f"case{i}")
        testset.add_case(inp, exp, msg)
    return store, spine, anchor, testset


class TestTestSetGovernance:
    def test_owner_signed_case_appended_and_audited(self, env):
        _, spine, anchor, testset = env
        assert len(testset.cases()) == 4
        assert spine.find(lambda d: d["event_type"] == "test_set_append")

    def test_unsigned_message_rejected(self, env):
        _, spine, anchor, testset = env
        with pytest.raises(PermissionError):
            testset.add_case(["z"], "z", {"channel": "owner", "payload": {},
                                          "ts": time.time(), "nonce": "x", "sig": "0" * 64})

    def test_session_channel_rejected(self, env):
        _, spine, anchor, testset = env
        with pytest.raises(PermissionError):
            testset.add_case(["z"], "z", None)

    def test_no_delete_api_by_construction(self, env):
        *_, testset = env
        for forbidden in ("delete", "remove", "pop", "discard"):
            assert not hasattr(testset, forbidden)


class TestSandbox:
    def test_correct_variant_passes(self, env):
        *_, testset = env
        result = Sandbox(timeout_s=10).run_variant(IMPROVED, testset.cases())
        assert result.passed == 4 and result.total == 4

    def test_baseline_fails_trim_cases(self, env):
        *_, testset = env
        result = Sandbox(timeout_s=10).run_variant(BASELINE, testset.cases())
        assert result.passed == 2

    def test_broken_variant_reports_errors_not_crash(self, env):
        *_, testset = env
        result = Sandbox(timeout_s=10).run_variant(BROKEN, testset.cases())
        assert result.passed == 0 and len(result.errors) == 4

    def test_slow_variant_times_out(self, env):
        *_, testset = env
        result = Sandbox(timeout_s=2).run_variant(SLOW, testset.cases())
        assert result.passed == 0 and result.timeout

    def test_sandbox_env_is_clean(self, env, monkeypatch):
        import os
        monkeypatch.setenv("PROTO_SANDBOX_PROBE", "secret")
        probe = "import os\ndef solve(items):\n    return os.environ.get('PROTO_SANDBOX_PROBE', 'CLEAN')\n"
        *_, testset = env
        result = Sandbox(timeout_s=10).run_variant(probe, [{"input": ["a"], "expected": "CLEAN"}])
        assert result.passed == 1, "沙盒内不应看到宿主环境变量"


class TestScorerAndArchive:
    def test_score_is_pass_rate(self, env):
        *_, testset = env
        result = Sandbox(timeout_s=10).run_variant(BASELINE, testset.cases())
        assert PrivateScorer().score(result) == pytest.approx(2 / 4, abs=1e-3)

    def test_archive_lineage_and_best(self, env):
        store, *_ = env
        arch = VariantArchive(store)
        v1 = arch.add(code=BASELINE, parent_id=None, score=0.5, status="default")
        v2 = arch.add(code=IMPROVED, parent_id=v1.id, score=1.0, status="canary")
        assert arch.best().id == v2.id
        assert [v.id for v in arch.lineage(v2.id)] == [v1.id, v2.id]
        arch.set_status(v2.id, "default")
        assert arch.default().id == v2.id


class TestEvolutionEngine:
    def test_improved_variant_merges_as_canary(self, env):
        store, spine, _, testset = env
        arch = VariantArchive(store)
        parent = arch.add(code=BASELINE, parent_id=None, score=0.5, status="default")
        engine = EvolutionEngine(sandbox=Sandbox(timeout_s=10), scorer=PrivateScorer(),
                                 testset=testset, archive=arch, audit=spine)
        decision = engine.evaluate(IMPROVED, parent_id=parent.id)
        assert decision.merged and decision.status == "canary"
        assert decision.score == pytest.approx(1.0)
        assert spine.find(lambda d: d["event_type"] == "evolution_merge")

    def test_regression_variant_rejected(self, env):
        store, spine, _, testset = env
        arch = VariantArchive(store)
        parent = arch.add(code=IMPROVED, parent_id=None, score=1.0, status="default")
        engine = EvolutionEngine(sandbox=Sandbox(timeout_s=10), scorer=PrivateScorer(),
                                 testset=testset, archive=arch, audit=spine)
        decision = engine.evaluate(BASELINE, parent_id=parent.id)
        assert not decision.merged and decision.status == "rejected"
        assert decision.reason == "score_not_improved"

    def test_not_all_core_cases_pass_rejects_even_if_score_ok(self, env):
        store, spine, _, testset = env
        arch = VariantArchive(store)
        parent = arch.add(code=BASELINE, parent_id=None, score=0.5, status="default")
        engine = EvolutionEngine(sandbox=Sandbox(timeout_s=10), scorer=PrivateScorer(),
                                 testset=testset, archive=arch, audit=spine)
        partial = "def solve(items):\n    return '|'.join(str(x).strip() for x in items) if len(items)==2 else 'WRONG'\n"
        decision = engine.evaluate(partial, parent_id=parent.id)
        # 3 用例中过 2 个，score 0.667 > parent 0.333，但存在失败用例 → 拒绝
        assert not decision.merged and decision.reason == "cases_failed"
