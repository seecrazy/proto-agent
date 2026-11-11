# -*- coding: utf-8 -*-
"""T1: 评审遗留 minor 的行为修复（M2-M8）。"""
import typing

import pytest

from proto.kernel.anchor import OwnerAnchor
from proto.kernel.audit import AuditSpine
from proto.kernel.budgets import BudgetPools
from proto.kernel.kernel import Kernel
from proto.state.agent_state import AgentState
from proto.state.store import FileSystemStore


@pytest.fixture()
def env(tmp_path):
    store = FileSystemStore(tmp_path / "data")
    spine = AuditSpine(store)
    anchor = OwnerAnchor.enroll(store, spine, b"\x01" * 32)
    pools = BudgetPools({"global": {"tokens": 1000.0}, "owner_tasks": {"tokens": 800.0},
                         "self_initiated": {"tokens": 200.0}})
    return store, spine, anchor, Kernel(anchor, spine, pools)


class TestM3TypeHints:
    def test_audit_find_type_hints_resolvable(self):
        from proto.kernel.audit import AuditSpine
        hints = typing.get_type_hints(AuditSpine.find)
        assert "predicate" in hints and "return" in hints


class TestM4PathTraversal:
    def test_dotdot_collection_rejected(self, tmp_path):
        store = FileSystemStore(tmp_path / "data")
        with pytest.raises(ValueError):
            store.put("../evil", "x", {})

    def test_dotdot_doc_id_rejected(self, tmp_path):
        store = FileSystemStore(tmp_path / "data")
        with pytest.raises(ValueError):
            store.put("ok", "../evil", {})


class TestM6KernelSurface:
    def test_public_trip_breaker(self, env):
        _, _, _, kernel = env
        kernel.trip_breaker()
        assert kernel.pools.breaker_tripped
        assert kernel.audit.find(lambda d: d["event_type"] == "budget_trip")


class TestM7AnchorCeremony:
    def test_enroll_refuses_silent_replacement(self, env):
        store, spine, anchor, _ = env
        with pytest.raises(PermissionError):
            OwnerAnchor.enroll(store, spine, b"\x02" * 32)

    def test_enroll_recovery_ceremony_allowed_and_audited(self, env):
        store, spine, anchor, _ = env
        new_anchor = OwnerAnchor.enroll(store, spine, b"\x03" * 32, recovery=True)
        rows = spine.find(lambda d: d["event_type"] == "anchor_init")
        assert rows[-1]["payload"].get("replaced") == anchor.fingerprint
        assert new_anchor.fingerprint != anchor.fingerprint

    def test_rotate_rejects_stale_countersign(self, tmp_path):
        import hashlib
        import time
        store = FileSystemStore(tmp_path / "data")
        spine = AuditSpine(store)
        secret = b"\x01" * 32
        anchor = OwnerAnchor.enroll(store, spine, secret)
        new_secret = b"\x09" * 32
        target = "sha256:" + hashlib.sha256(new_secret).hexdigest()
        stale = OwnerAnchor._sign_with(secret, {"rotate_to": target}, 1.0, "r1")  # ts=1.0 早已过期
        with pytest.raises(PermissionError):
            anchor.rotate(new_secret, stale)
        fresh = OwnerAnchor._sign_with(secret, {"rotate_to": target}, time.time(), "r2")
        anchor.rotate(new_secret, fresh)  # 对照：新鲜会签可轮换


class TestM8BudgetsAndGoals:
    def test_unknown_unit_denied_without_tripping(self, env):
        _, _, _, kernel = env
        d = kernel.pools.try_consume("owner_tasks", {"widgets": 5})
        assert not d.granted and "unknown_unit" in d.reason
        assert not kernel.pools.breaker_tripped

    def test_goal_denials_audited(self, env, tmp_path):
        from proto.metacog.goals import Goal, GoalArbiter
        _, spine, _, kernel = env
        arb = GoalArbiter(kernel, owner_priority="x")
        g = Goal(description="自主探索", desire_dim="curiosity_gap", gap=0.9,
                 feasibility=0.7, interest=0.9, verification="笔记")
        arb.push_self_initiated(g, cost={"tokens": 300})  # 超池 → 拒绝
        rows = spine.find(lambda d: d["event_type"] == "denial")
        assert rows and rows[-1]["payload"]["reason"].startswith("self_initiated")


class TestM8StateHygiene:
    def test_to_dict_returns_deep_copy(self):
        state = AgentState()
        raw = state.to_dict()
        raw["instincts"]["exploration"]["value"] = 99.0
        assert state.get("instincts.exploration") != 99.0

    def test_accumulator_requires_audit(self):
        from proto.state.modulation import AttributionAccumulator
        with pytest.raises(TypeError):
            AttributionAccumulator(threshold=0.3)


class TestM6HarnessMetrics:
    def test_parametric_policy_reports_verification_rounds(self):
        from proto.evalx.harness import EvalHarness, ParametricPolicy
        from proto.exec.injector import StateInjector
        state = AgentState()
        ctx = StateInjector().render("T", state, [])
        record = ParametricPolicy().act(
            {"name": "T", "tools": ["a", "b", "c"]}, ctx, __import__("random").Random(1))
        assert "verification_rounds" in record

    def test_eval_default_runs_is_200(self):
        from proto.evalx.harness import EvalHarness
        assert EvalHarness().runs == 200

    def test_injector_golden_text(self):
        from proto.exec.injector import StateInjector
        state = AgentState()
        golden = ("[任务] 示例\n"
                  "[内在状态]\n"
                  "exploration:0.6/0.9 risk_tolerance:0.4/0.7 social_initiative:0.5/0.9 perfectionism:0.5/0.9\n"
                  "rigor:0.7 proactiveness:0.5 directness:0.5 independence:0.5\n"
                  "curiosity_gap:0.3 goal_urgency:0.7(高) social_connection_gap:0.4 recognition_need:0.4\n"
                  "pleasure=0 arousal=0.4 dominance=0.5\n"
                  "[认知调制]\n"
                  "k=8 ε=0.122 bias=0 th=0.54\n"
                  "[行为策略]\n"
                  "(无)")
        assert StateInjector().render("示例", state, []) == golden
