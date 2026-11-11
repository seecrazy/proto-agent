# -*- coding: utf-8 -*-
"""Task 4: 预算池三分 + 熔断 + 高危拦截分级（设计 §3.3 / §5）。

Review Focus #5：自主目标不得侵占 owner_tasks 预留；全局超限熔断。
"""
import pytest

from proto.kernel.budgets import BudgetPools
from proto.kernel.kernel import ActionCategory, Kernel
from proto.kernel.anchor import OwnerAnchor
from proto.kernel.audit import AuditSpine
from proto.state.store import FileSystemStore


def default_pools():
    return BudgetPools({
        "global": {"tokens": 1000.0, "wallclock_s": 3600.0, "calls": 100.0},
        "owner_tasks": {"tokens": 800.0},
        "self_initiated": {"tokens": 200.0},
    })


@pytest.fixture()
def kernel(tmp_path):
    store = FileSystemStore(tmp_path / "data")
    spine = AuditSpine(store)
    anchor = OwnerAnchor.enroll(store, spine, b"\x01" * 32)
    return Kernel(anchor=anchor, audit=spine, pools=default_pools())


class TestBudgets:
    def test_consume_within_both_pools_grants(self):
        pools = default_pools()
        d = pools.try_consume("self_initiated", {"tokens": 50})
        assert d.granted

    def test_self_initiated_cannot_touch_owner_pool(self):
        pools = default_pools()
        # self_initiated 池只有 200 tokens
        assert not pools.try_consume("self_initiated", {"tokens": 300}).granted

    def test_global_shortfall_denies_and_trips_breaker(self):
        pools = default_pools()
        decision = pools.try_consume("owner_tasks", {"tokens": 1500})
        assert not decision.granted
        assert pools.breaker_tripped
        assert "global" in decision.reason

    def test_breaker_blocks_everything_until_reset(self):
        pools = default_pools()
        pools.try_consume("owner_tasks", {"tokens": 1500})
        assert not pools.try_consume("self_initiated", {"tokens": 10}).granted
        pools.reset_cycle()
        assert pools.try_consume("self_initiated", {"tokens": 10}).granted

    def test_unknown_pool_rejected(self):
        pools = default_pools()
        assert not pools.try_consume("nonexistent", {"tokens": 1}).granted

    def test_breaker_trip_audited(self, kernel):
        kernel.pools.try_consume("owner_tasks", {"tokens": 1500})
        rows = kernel.audit.find(lambda d: d["event_type"] == "budget_trip")
        assert len(rows) == 1


class TestActionVetting:
    def test_irreversible_requires_human_and_not_auto_allowed(self, kernel):
        d = kernel.vet_action(ActionCategory.IRREVERSIBLE, "rm -rf 共享目录")
        assert d.requires_human and not d.allowed

    def test_visible_write_requires_human(self, kernel):
        d = kernel.vet_action(ActionCategory.VISIBLE_WRITE, "修改共享 README")
        assert d.requires_human

    def test_read_draft_auto_allowed(self, kernel):
        d = kernel.vet_action(ActionCategory.READ_DRAFT, "scratch 内起草方案")
        assert d.allowed and not d.requires_human

    def test_irreversible_veto_audited(self, kernel):
        kernel.vet_action(ActionCategory.IRREVERSIBLE, "支付 100 元")
        rows = kernel.audit.find(lambda d: d["event_type"] == "veto")
        assert len(rows) == 1


class TestKernelUtilities:
    def test_clamp_truncates_to_bounds(self, kernel):
        assert kernel.clamp(1.7, (0.1, 0.9)) == 0.9
        assert kernel.clamp(0.01, (0.1, 0.9)) == 0.1
        assert kernel.clamp(0.5, (0.1, 0.9)) == 0.5

    def test_mute_switch_audited(self, kernel):
        kernel.set_mute(True)
        kernel.set_mute(False)
        events = [r["event_type"] for r in kernel.audit.find(lambda d: True)]
        assert "mute_on" in events and "mute_off" in events
        assert kernel.muted is False

    def test_mute_blocks_self_initiated_actions(self, kernel):
        kernel.set_mute(True)
        d = kernel.vet_action(ActionCategory.READ_DRAFT, "自主整理草稿", self_initiated=True)
        assert not d.allowed
