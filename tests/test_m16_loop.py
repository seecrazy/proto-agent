# -*- coding: utf-8 -*-
"""M16b: AgenticLoop（观察-行动循环，遇审批可暂停恢复）+ 外部 agent 桥。"""
import json
import stat

import pytest

from proto.exec.agentic import AgenticLoop
from proto.llm.client import LLMClient


class LoopLLM(LLMClient):
    """脚本化循环决策：依次吐出工具调用，最后 done。"""
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.prompts = []
    def complete(self, system, user):
        if "归因" in system:
            return "[]"
        self.prompts.append(user)
        item = self.decisions.pop(0)
        return item if isinstance(item, str) else json.dumps(item)


@pytest.fixture()
def agent(tmp_path):
    from proto.exec.webtools import WebToolkit
    from proto.runtime.agent import ProtoAgent
    dead = WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))
    return ProtoAgent(data_dir=tmp_path, llm=LoopLLM([]), goal_interval_s=10**9,
                      webtools=dead)


class TestAgenticLoop:
    def test_observe_act_until_done(self, agent):
        agent.llm.decisions = [
            {"tool": "write_file", "args": {"path": "calc.py", "content": "print(6*7)"},
             "category": "workspace_write"},
            {"tool": "run_python", "args": {"code": "print(6*7)"}, "category": "read_draft"},
            {"done": True, "summary": "写好并验证了 calc.py，输出 42"},
        ]
        loop = AgenticLoop(agent, max_steps=8)
        result = loop.run("写一个计算脚本并验证")
        assert result["status"] == "completed"
        assert "42" in result["summary"] or result["observations"]
        assert (agent.code_workspace.root / "calc.py").exists()
        assert any("42" in str(o) for o in result["observations"])

    def test_loop_prompt_carries_observations(self, agent):
        agent.llm.decisions = [
            {"tool": "run_python", "args": {"code": "print(1)"}, "category": "read_draft"},
            {"done": True, "summary": "ok"},
        ]
        AgenticLoop(agent, max_steps=4).run("t")
        second_prompt = agent.llm.prompts[1]
        assert "print(1)" in second_prompt or "1" in second_prompt  # 观察回灌

    def test_paused_on_visible_write_resumable(self, agent):
        agent.llm.decisions = [
            {"tool": "write_file",
             "args": {"path": "shared.md", "content": "对外内容"},
             "category": "visible_write"},
        ]
        loop = AgenticLoop(agent, max_steps=4)
        result = loop.run("发布文档")
        assert result["status"] == "paused"
        final = agent.resume_task(result["task_id"], approved=True)
        assert final["status"] == "completed"
        assert agent.workspace.list_visible() == ["shared.md"]   # 批准即晋升

    def test_max_steps_reached(self, agent):
        agent.llm.decisions = [
            {"tool": "run_python", "args": {"code": "print(1)"}, "category": "read_draft"},
        ] * 10
        result = AgenticLoop(agent, max_steps=3).run("t")
        assert result["status"] == "max_steps"

    def test_offline_returns_no_llm(self, tmp_path, agent):
        from proto.exec.webtools import WebToolkit
        from proto.runtime.agent import ProtoAgent
        plain = ProtoAgent(data_dir=tmp_path / "x", goal_interval_s=10**9,
                           webtools=WebToolkit(fetch=lambda u, a="t":
                                               (_ for _ in ()).throw(OSError("禁网"))))
        assert AgenticLoop(plain).run("t")["status"] == "no_llm"


class TestAgentTaskUsesLoop:
    def test_run_task_uses_agentic_loop(self, agent):
        agent.llm.decisions = [
            {"tool": "run_python", "args": {"code": "print('proto')"}, "category": "read_draft"},
            {"done": True, "summary": "执行完成"},
        ]
        result = agent.run_task("跑一段打印")
        assert result["status"] == "completed"
        assert any("proto" in str(o) for o in result.get("observations", []))


class TestCodingBridge:
    def test_detects_and_invokes_external_cli(self, tmp_path, agent):
        from proto.exec.bridge import CodingBridge
        fake = tmp_path / "fakeclaude"
        fake.write_text('#!/bin/sh\necho "CLAUDE_SAYS_OK"\n')
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        bridge = CodingBridge(cli_path=str(fake), cli_name="claude")
        assert bridge.available
        out = bridge.delegate("修复登录超时")
        assert "CLAUDE_SAYS_OK" in out
        assert not CodingBridge(cli_path=None).available
