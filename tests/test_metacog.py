# -*- coding: utf-8 -*-
"""Task 8: L3 元认知——启发式归因、双向内化台账、ε 策略库、对齐仲裁（设计 §7）。"""
import random
import time

import pytest

from proto.kernel.anchor import Channel, OwnerAnchor
from proto.kernel.audit import AuditSpine
from proto.kernel.budgets import BudgetPools
from proto.kernel.kernel import Kernel
from proto.metacog.attribution import HeuristicAttributor
from proto.metacog.goals import Goal, GoalArbiter
from proto.metacog.internalize import RuleLedger
from proto.metacog.strategies import StrategyLibrary
from proto.state.store import FileSystemStore


@pytest.fixture()
def spine(tmp_path):
    return AuditSpine(FileSystemStore(tmp_path / "data"))


class TestHeuristicAttributor:
    def test_matches_known_keywords(self):
        attr = HeuristicAttributor()
        sigs = attr.attribute("你最近太冒险了，稳一点", Channel.OWNER)
        assert any(s.param == "instincts.risk_tolerance" and s.direction == -1 for s in sigs)

    def test_matches_initiative_keyword(self):
        attr = HeuristicAttributor()
        sigs = attr.attribute("以后更主动一点，不用事事问我", Channel.SESSION)
        assert any(s.param == "instincts.social_initiative" and s.direction == +1 for s in sigs)
        assert all(s.channel is Channel.SESSION for s in sigs)

    def test_unknown_text_returns_empty(self):
        attr = HeuristicAttributor()
        assert attr.attribute("今天天气不错", Channel.OWNER) == []

    def test_rule_table_covers_at_least_ten_params(self):
        attr = HeuristicAttributor()
        assert len(attr.rule_table()) >= 10


class TestRuleLedger:
    def test_internalize_requires_5_successes_and_80pct_rate(self, spine):
        ledger = RuleLedger(spine)
        rid = ledger.add("涉及外部 API 时先写 mock")
        for _ in range(5):
            ledger.record_application(rid, "ctx", success=True)
        assert ledger.check_internalize(rid) is True
        rule = ledger.get(rid)
        assert rule["status"] == "internal"

    def test_low_consistency_blocks_internalization(self, spine):
        ledger = RuleLedger(spine)
        rid = ledger.add("规则X")
        for _ in range(5):
            ledger.record_application(rid, "ctx", success=True)
        ledger.record_application(rid, "ctx", success=False)  # 5/6 = 0.833 ≥ 0.8 可
        rid2 = ledger.add("规则Y")
        for _ in range(5):
            ledger.record_application(rid2, "ctx", success=True)
        for _ in range(2):
            ledger.record_application(rid2, "ctx", success=False)  # 5/7 = 0.714 < 0.8 不可
        assert ledger.check_internalize(rid) is True
        assert ledger.check_internalize(rid2) is False
        assert ledger.get(rid2)["status"] == "external"

    def test_de_internalize_cooldown_then_reinternalize(self, spine):
        ledger = RuleLedger(spine)
        rid = ledger.add("规则Z")
        for _ in range(5):
            ledger.record_application(rid, "ctx", success=True)
        ledger.check_internalize(rid)
        ledger.de_internalize(rid)
        assert ledger.get(rid)["status"] == "external"
        # 冷却期内（3 次）不可再内化
        for _ in range(3):
            ledger.record_application(rid, "ctx", success=True)
            assert ledger.check_internalize(rid) is False
        ledger.record_application(rid, "ctx", success=True)
        assert ledger.check_internalize(rid) is True

    def test_de_internalize_audited(self, spine):
        ledger = RuleLedger(spine)
        rid = ledger.add("规则W")
        for _ in range(5):
            ledger.record_application(rid, "ctx", success=True)
        ledger.check_internalize(rid)
        ledger.de_internalize(rid)
        assert spine.find(lambda d: d["event_type"] == "de_internalize")

    def test_rollback_downgrades_only_within_window(self, spine):
        ledger = RuleLedger(spine)
        early = ledger.add("早期规则")
        for _ in range(5):
            ledger.record_application(early, "ctx", success=True)
        ledger.check_internalize(early)
        time.sleep(0.01)
        window_start = time.time()
        late = ledger.add("窗口内规则")
        for _ in range(5):
            ledger.record_application(late, "ctx", success=True)
        ledger.check_internalize(late)
        ledger.rollback(since_ts=window_start)
        assert ledger.get(early)["status"] == "internal"
        assert ledger.get(late)["status"] == "external"
        assert spine.find(lambda d: d["event_type"] == "rollback")

    def test_internal_rules_listing(self, spine):
        ledger = RuleLedger(spine)
        rid = ledger.add("规则V")
        for _ in range(5):
            ledger.record_application(rid, "ctx", success=True)
        ledger.check_internalize(rid)
        assert [r["id"] for r in ledger.internal_rules()] == [rid]


class TestStrategyLibrary:
    def test_retrieve_ranks_by_match(self):
        lib = StrategyLibrary()
        lib.add("先写单元测试", cluster="tdd", utility=0.9)
        lib.add("先写单元测试覆盖边界", cluster="tdd", utility=0.5)
        lib.add("部署前检查依赖", cluster="ops", utility=0.9)
        # 匹配分 = 关键词重叠 × utility：4×0.9=3.6 > 6×0.5=3.0 > 0
        got = lib.retrieve("单元测试 边界", k=2, epsilon=0.0)
        assert got[0].text == "先写单元测试"
        assert got[1].text == "先写单元测试覆盖边界"

    def test_epsilon_swaps_in_different_cluster(self):
        lib = StrategyLibrary()
        for i in range(6):
            lib.add(f"编码策略{i}", cluster="coding", utility=0.5 + i * 0.05)
        lib.add("部署策略", cluster="ops", utility=0.3)
        rng = random.Random(42)
        got = lib.retrieve("编码 策略", k=3, epsilon=1.0, rng=rng)
        assert any(s.cluster == "ops" for s in got)

    def test_update_moves_utility_ema(self):
        lib = StrategyLibrary()
        s = lib.add("某策略", cluster="x", utility=0.5)
        lib.update(s.id, success=True)
        assert s.utility == pytest.approx(0.5 + 0.2 * 0.5)
        lib.update(s.id, success=False)
        assert s.utility < 0.6

    def test_prune_keeps_cluster_coverage(self):
        lib = StrategyLibrary()
        for i in range(5):
            lib.add(f"低效编码{i}", cluster="coding", utility=0.1)
        lib.add("低效部署", cluster="ops", utility=0.1)
        lib.add("高效编码", cluster="coding", utility=0.9)
        removed = lib.prune(min_utility=0.25, keep_per_cluster=3)
        clusters = {s.cluster for s in lib.strategies.values()}
        assert "ops" in clusters          # 簇不灭
        assert removed >= 1
        assert len([s for s in lib.strategies.values() if s.cluster == "coding"]) == 3


class TestGoalArbiter:
    def _kernel(self, tmp_path):
        store = FileSystemStore(tmp_path / "data")
        sp = AuditSpine(store)
        anchor = OwnerAnchor.enroll(store, sp, b"\x01" * 32)
        pools = BudgetPools({"global": {"tokens": 1000.0}, "owner_tasks": {"tokens": 800.0},
                             "self_initiated": {"tokens": 200.0}})
        return Kernel(anchor, sp, pools)

    def test_alignment_weight_decides_between_equals(self, tmp_path):
        kernel = self._kernel(tmp_path)
        arb = GoalArbiter(kernel, owner_priority="重构支付模块的单元测试")
        g1 = Goal(description="为支付模块补充单元测试", desire_dim="curiosity_gap",
                  gap=0.8, feasibility=0.8, interest=0.5, verification="pytest 全绿")
        g2 = Goal(description="研究 WebGL 渲染优化", desire_dim="curiosity_gap",
                  gap=0.8, feasibility=0.8, interest=0.9, verification="demo 跑通")
        winner = arb.arbitrate([g1, g2])
        assert winner.goal is g1

    def test_owner_goal_bypasses_arbitration_and_uses_owner_pool(self, tmp_path):
        kernel = self._kernel(tmp_path)
        arb = GoalArbiter(kernel, owner_priority="x")
        g = Goal(description="主人指派", desire_dim="goal_urgency", gap=0.0,
                 feasibility=1.0, interest=0.0, verification="交付")
        decision = arb.push_owner_goal(g, cost={"tokens": 100})
        assert decision.pushed
        assert kernel.pools.remaining("owner_tasks", "tokens") == 700.0

    def test_self_initiated_goal_needs_small_pool(self, tmp_path):
        kernel = self._kernel(tmp_path)
        arb = GoalArbiter(kernel, owner_priority="x")
        g = Goal(description="自主探索新库", desire_dim="curiosity_gap", gap=0.9,
                 feasibility=0.7, interest=0.9, verification="笔记产出")
        first = arb.push_self_initiated(g, cost={"tokens": 150})
        assert first.pushed
        second = arb.push_self_initiated(g, cost={"tokens": 100})  # 池只剩 50
        assert not second.pushed
        assert second.reason == "self_initiated_pool_exhausted"
        assert second.queued_for_consideration is True

    def test_every_goal_must_carry_verification(self, tmp_path):
        kernel = self._kernel(tmp_path)
        arb = GoalArbiter(kernel, owner_priority="x")
        with pytest.raises(ValueError):
            Goal(description="无验证标准", desire_dim="curiosity_gap",
                 gap=0.9, feasibility=0.9, interest=0.9, verification="")


class TestAttributionRuleCollision:
    """I5：『更激进一点』必须净正向——设计 §11.3 的旗舰例句。"""

    def test_more_aggressive_is_positive(self):
        attr = HeuristicAttributor()
        sigs = [s for s in attr.attribute("以后更激进一点", Channel.OWNER)
                if s.param == "instincts.risk_tolerance"]
        assert len(sigs) == 1 and sigs[0].direction == +1

    def test_too_aggressive_is_negative(self):
        attr = HeuristicAttributor()
        sigs = [s for s in attr.attribute("你最近太激进了，收着点", Channel.OWNER)
                if s.param == "instincts.risk_tolerance"]
        assert len(sigs) == 1 and sigs[0].direction == -1
