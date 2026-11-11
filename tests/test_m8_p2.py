# -*- coding: utf-8 -*-
"""M8-D(P2): 备份命令 + evalx LLM 策略适配。"""
import json
import zipfile

import pytest

from proto.runtime.agent import ProtoAgent


class FakeLLM:
    def complete(self, system, user):
        if "工具选择" in system or "tool" in system.lower():
            return json.dumps({"tools": ["搜索", "搜索", "读文件"],
                               "risky": False, "verify_rounds": 3})
        return "[]"


class TestBackup:
    def test_backup_zip_contains_key_and_state(self, tmp_path):
        from proto.backup import create_backup

        agent_dir = tmp_path / "agent"
        ProtoAgent(data_dir=agent_dir)  # 生成 owner.key + 状态
        out = tmp_path / "backup.zip"
        fingerprint = create_backup(agent_dir, out)
        assert fingerprint.startswith("sha256:")
        assert out.exists()
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        assert "owner.key" in names
        assert any("state" in n for n in names)


class TestLLMPolicy:
    def test_act_parses_model_choices(self, tmp_path):
        from proto.evalx.llm_policy import LLMPolicy

        policy = LLMPolicy(FakeLLM())
        record = policy.act({"name": "调研", "tools": ["搜索", "读文件", "执行"]},
                            "任意上下文", __import__("random").Random(1))
        assert record["tools_used"] == ["搜索", "搜索", "读文件"]
        assert record["verification_rounds"] == 3
        assert record["risky_choice"] == 0.0

    def test_defensive_fallback_on_bad_json(self, tmp_path):
        from proto.evalx.llm_policy import LLMPolicy

        class BadLLM:
            def complete(self, s, u):
                return "我无法回答"

        policy = LLMPolicy(BadLLM())
        record = policy.act({"name": "t", "tools": ["a", "b"]}, "ctx",
                            __import__("random").Random(1))
        assert len(record["tools_used"]) > 0   # 回退为确定性选择

    def test_harness_measures_llm_policy_distributions(self):
        """两个状态快照下，LLM 策略的 behavior 应产生可对比的分布。"""
        from proto.evalx.harness import EvalHarness
        from proto.evalx.llm_policy import LLMPolicy
        from proto.state.agent_state import AgentState

        tasks = [{"name": f"任务{i}", "tools": ["a", "b", "c", "d"]} for i in range(5)]
        report = EvalHarness(runs_per_cell=5).run(
            LLMPolicy(FakeLLM()), tasks, {"s1": AgentState(), "s2": AgentState()})
        # FakeLLM 固定选择 → 两快照分布相同（d=0）；接口联通性验证
        assert abs(report.effect("s1", "s2", "tool_diversity")) == pytest.approx(0.0)
