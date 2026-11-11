# -*- coding: utf-8 -*-
"""M4-③: L3 运行节奏调度器（每任务蒸馏/定时目标生成/每日深度回顾）+ 策略蒸馏接线（设计 §7.3/L3 运行节奏）。"""
import pytest

from proto.metacog.distill import TaskDistiller
from proto.metacog.scheduler import MetacogScheduler
from proto.metacog.strategies import StrategyLibrary
from proto.exec.router import TaskResult


@pytest.fixture()
def scheduler():
    s = MetacogScheduler(goal_interval_s=4 * 3600, deep_review_interval_s=86400,
                         clock=lambda: 1000.0)
    s.start()
    return s


class TestScheduler:
    def test_no_fire_at_start(self, scheduler):
        assert scheduler.tick() == []

    def test_goal_generation_fires_on_interval(self, scheduler):
        assert scheduler.tick(now=1000.0 + 4 * 3600) == ["goal_generation"]
        # 一个周期内不重复触发
        assert scheduler.tick(now=1000.0 + 4 * 3600 + 60) == []

    def test_next_cycle_fires_again(self, scheduler):
        scheduler.tick(now=1000.0 + 4 * 3600)
        assert scheduler.tick(now=1000.0 + 8 * 3600) == ["goal_generation"]

    def test_deep_review_fires_daily(self, scheduler):
        fired = scheduler.tick(now=1000.0 + 86400)
        assert "deep_review" in fired and "goal_generation" in fired

    def test_callbacks_invoked(self, scheduler):
        calls = []
        scheduler.register("goal_generation", lambda: calls.append("goal"))
        scheduler.register("deep_review", lambda: calls.append("review"))
        scheduler.tick(now=1000.0 + 86400)
        assert calls == ["goal", "review"]

    def test_task_completion_triggers_distill_immediately(self, scheduler):
        calls = []
        scheduler.register("distill", lambda result: calls.append(result.task_id))
        scheduler.on_task_completed(TaskResult("t9", "completed"))
        assert calls == ["t9"]


class TestTaskDistiller:
    def test_success_with_retry_distills_positive_strategy(self):
        lib = StrategyLibrary()
        d = TaskDistiller(lib)
        result = TaskResult("t1", "completed", retries=2)
        strat = d.distill(result, domain="integration")
        assert "重试" in strat.text and strat.cluster == "integration"
        assert strat.valence > 0
        assert any("重试" in s.text for s in lib.retrieve("重试", k=3, epsilon=0.0))

    def test_failure_distills_conditional_lesson(self):
        lib = StrategyLibrary()
        d = TaskDistiller(lib)
        result = TaskResult("t2", "failed", reason="unknown_tool:nope")
        strat = d.distill(result, domain="ops")
        assert "避免" in strat.text and strat.valence < 0

    def test_plain_success_no_distillation(self):
        lib = StrategyLibrary()
        d = TaskDistiller(lib)
        assert d.distill(TaskResult("t3", "completed"), domain="x") is None
        assert len(lib.strategies) == 0

    def test_denied_task_distills_lesson(self):
        lib = StrategyLibrary()
        d = TaskDistiller(lib)
        strat = d.distill(TaskResult("t4", "denied", reason="human_denied"), domain="publish")
        assert strat is not None and strat.valence < 0
