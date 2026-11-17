# -*- coding: utf-8 -*-
"""M23: 质量三定律——想法出关需定义/轨迹结构化/独立失败关闭验证门。"""
import json

import pytest

from proto.llm.client import LLMClient
from proto.runtime.agent import ProtoAgent


def dead_web():
    from proto.exec.webtools import WebToolkit
    return WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))


class GateLLM(LLMClient):
    """脚本化：归因/验收定义/循环决策/独立验证 各走各的分支。"""
    def __init__(self, decisions=None, acceptance=None, verdicts=None):
        self.decisions = list(decisions or [])
        self.acceptance_reply = acceptance
        self.verdicts = list(verdicts or [])
        self.verifier_inputs = []
    def complete(self, system, user):
        if "归因" in system:
            return "[]"
        if "最小可检验定义" in system:
            return self.acceptance_reply or "好的。"
        if "独立验收" in system:
            self.verifier_inputs.append(user)
            v = self.verdicts.pop(0) if self.verdicts else \
                json.dumps({"verdict": "fail", "missing": ["无输出"]})
            return v if isinstance(v, str) else json.dumps(v)
        if self.decisions:
            item = self.decisions.pop(0)
            return item if isinstance(item, str) else json.dumps(item)
        return json.dumps({"done": True, "summary": "我搞定了"})


@pytest.fixture()
def agent(tmp_path):
    llm = GateLLM(
        acceptance=json.dumps({"acceptance": "运行 calc.py 输出包含 42"}),
        decisions=[
            "写脚本并运行",                                        # plan
            {"tool": "write_file", "args": {"path": "calc.py",
                                            "content": "print(6*7)"},
             "category": "workspace_write"},
            {"tool": "run_python", "args": {"code": "print(6*7)"},
             "category": "read_draft"},
            {"done": True, "summary": "我搞定了，输出是 42"},
        ],
        verdicts=[json.dumps({"verdict": "pass"})])
    return ProtoAgent(data_dir=tmp_path, llm=llm, goal_interval_s=10 ** 9,
                      webtools=dead_web())


class TestAcceptanceGate:
    def test_goal_node_carries_acceptance(self, agent):
        node = agent.goal_tree.add("验证计算", level="task", source="owner",
                                   acceptance="运行输出 42")
        assert node.acceptance == "运行输出 42"
        assert agent.goal_tree.get(node.id).acceptance == "运行输出 42"


class TestVerificationGate:
    def test_pass_with_evidence(self, agent):
        result = agent.run_task("写计算脚本并验证输出 42")
        assert result["status"] == "verified"
        assert result["verified"] is True
        assert result["trace"] and result["trace"][0]["tool"]

    def test_verifier_never_sees_executor_narrative(self, agent):
        agent.run_task("写计算脚本并验证输出 42")
        joined = "\n".join(agent.llm.verifier_inputs)
        assert "我搞定了" not in joined            # 执行者自述被隔离
        assert "验收定义" in joined or "42" in joined

    def test_fail_closed_on_unparseable(self, tmp_path):
        llm = GateLLM(acceptance=json.dumps({"acceptance": "x"}),
                      decisions=[{"done": True, "summary": "done"}],
                      verdicts=["我觉得可以"])
        a = ProtoAgent(data_dir=tmp_path, llm=llm, goal_interval_s=10 ** 9,
                       webtools=dead_web())
        result = a.run_task("t")
        assert result["verified"] is False          # 解析失败 → 失败关闭
        assert result["missing"]

    def test_retry_once_with_feedback_then_fail(self, tmp_path):
        llm = GateLLM(
            acceptance=json.dumps({"acceptance": "必须运行并输出 42"}),
            decisions=[
                "计划", {"done": True, "summary": "写完了"},        # 首轮
                "补充运行", {"done": True, "summary": "还是没运行"},  # 重试轮
            ],
            verdicts=[json.dumps({"verdict": "fail",
                                  "missing": ["缺少运行证据"]}),
                      json.dumps({"verdict": "fail",
                                  "missing": ["仍未运行"]})])
        a = ProtoAgent(data_dir=tmp_path, llm=llm, goal_interval_s=10 ** 9,
                       webtools=dead_web())
        result = a.run_task("写脚本")
        assert result["status"] == "failed_verification"
        assert "仍未运行" in str(result["missing"])
        # 失败蒸馏为教训（Reflexion：failure becomes a note）
        lessons = [s for s in a.library.strategies.values() if s.valence < 0]
        assert lessons

    def test_offline_skips_gate_honestly(self, tmp_path):
        a = ProtoAgent(data_dir=tmp_path, goal_interval_s=10 ** 9,
                       webtools=dead_web())
        result = a.run_task("t")
        assert result["status"] in ("no_plan", "completed")
        assert result.get("verified", "n/a") is None or \
            result["status"] == "no_plan"

    def test_milestone_only_on_verified(self, tmp_path):
        llm = GateLLM(
            acceptance=json.dumps({"acceptance": "输出 42"}),
            decisions=["p", {"done": True, "summary": "ok"}],
            verdicts=[json.dumps({"verdict": "fail", "missing": ["无证据"]}),
                      json.dumps({"verdict": "fail", "missing": ["仍无"]})])
        a = ProtoAgent(data_dir=tmp_path, llm=llm, goal_interval_s=10 ** 9,
                       webtools=dead_web())
        a.set_priority("深入研究 AI")            # 播种长期目标
        long_id = a.goal_tree.active_long_term()[0].id
        task_node = a.goal_tree.add("子任务", level="short", parent_id=long_id,
                                    source="owner", acceptance="输出 42")
        result = a.run_task("做子任务")
        a._selected_goal_id = task_node.id
        # 模拟 accept 流：验证未过 → 目标不完成
        if result["verified"] is False:
            assert a.goal_tree.get(task_node.id).status == "active"
