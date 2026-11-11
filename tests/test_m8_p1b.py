# -*- coding: utf-8 -*-
"""M8-C(P1): 规矩内化闭环 + L5 进化运行时接口。"""
import time

import pytest

from proto.llm.client import LLMClient
from proto.runtime.agent import ProtoAgent


class NullLLM(LLMClient):
    def complete(self, system, user):
        return "[]" if "归因" in system else "好的。"


@pytest.fixture()
def agent(tmp_path):
    return ProtoAgent(data_dir=tmp_path, llm=NullLLM(), goal_interval_s=10 ** 9)


class TestRuleInternalization:
    def test_rule_internalizes_after_five_successful_tasks(self, agent):
        rid = agent.instruct("涉及支付改动必须先写测试")
        for _ in range(5):
            agent._on_task_completed(
                type("R", (), {"status": "completed"})())
        assert agent.ledger.get(rid)["status"] == "internal"
        assert agent.rules_view()[0]["status"] == "internal"

    def test_deinternalize_via_agent(self, agent):
        rid = agent.instruct("规矩")
        for _ in range(5):
            agent._on_task_completed(type("R", (), {"status": "completed"})())
        agent.deinternalize_rule(rid)
        assert agent.ledger.get(rid)["status"] == "external"


class TestEvolutionRuntime:
    def test_add_test_case_and_mutate(self, agent):
        agent.add_test_case([" a "], "a")
        decision = agent.evolve(
            "def solve(items):\n    return '|'.join(str(x).strip() for x in items)\n")
        assert decision["merged"] is True and decision["score"] == 1.0
        snapshot = agent.evolution_snapshot()
        assert snapshot["testset_count"] == 1
        assert snapshot["variants"]

    def test_promote_low_risk_to_default(self, agent):
        agent.add_test_case([" a "], "a")
        decision = agent.evolve(
            "def solve(items):\n    return '|'.join(str(x).strip() for x in items)\n")
        result = agent.promote_variant(decision["variant_id"], "prompt_template")
        assert result is True
        assert agent.evolution_snapshot()["variants"][-1]["status"] == "default"

    def test_promote_high_risk_denied(self, agent):
        agent.add_test_case([" a "], "a")
        decision = agent.evolve(
            "def solve(items):\n    return '|'.join(str(x).strip() for x in items)\n")
        assert agent.promote_variant(decision["variant_id"], "l1_rules") is False
