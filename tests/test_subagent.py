# -*- coding: utf-8 -*-
"""M78 子代理工具 run_subagent：能读能写工作区、白名单拒止、审计派发/返回粒度。"""
import json

import pytest

from proto.exec.agentic import LOOP_SYSTEM, AgenticLoop
from proto.exec.subagent import SYSTEM as SUBAGENT_SYSTEM, run_subagent
from proto.runtime.agent import ProtoAgent
from proto.tools.webtools import WebToolkit


class SubagentScriptModel:
    """按系统提示分流：主循环 / 子代理各自消费一份脚本决策。"""

    def __init__(self, loop=(), sub=()):
        self.loop = list(loop)
        self.sub = list(sub)
        self.sub_inputs = []

    def complete(self, system, user):
        if system == SUBAGENT_SYSTEM:
            self.sub_inputs.append(user)
            return json.dumps(self.sub.pop(0), ensure_ascii=False)
        if system == LOOP_SYSTEM:
            return json.dumps(self.loop.pop(0), ensure_ascii=False)
        raise AssertionError(system)


def make_agent(tmp_path, model):
    agent = ProtoAgent(
        data_dir=tmp_path / "data", llm=model,
        webtools=WebToolkit(fetch=lambda *a, **k: (_ for _ in ()).throw(OSError("offline"))),
        goal_interval_s=10 ** 9)
    agent.llm_fast = None
    return agent


def audit_kinds(agent):
    return [row["event_type"] for row in agent.spine.tail(30)]


def test_subagent_writes_file_inside_workspace_and_audits(tmp_path):
    model = SubagentScriptModel(
        loop=[{"tool": "run_subagent",
               "args": {"task": "在工作区写 notes.md 总结"}},
              {"done": True, "summary": "子代理已交付"}],
        sub=[{"tool": "write_file", "args": {"path": "projects/app/notes.md",
                                             "content": "# 结论\n认证集中在 z_auth.py"}},
             {"done": True, "summary": "已写 notes.md，含认证结论"}])
    agent = make_agent(tmp_path, model)
    (agent.code_workspace.root / "projects" / "app").mkdir(parents=True, exist_ok=True)
    result = AgenticLoop(agent, max_steps=4).run("让子代理写笔记", project_dir="projects/app")
    assert result["status"] == "completed"
    written = agent.code_workspace.root / "projects" / "app" / "notes.md"
    assert written.exists() and "认证集中" in written.read_text(encoding="utf-8")
    kinds = audit_kinds(agent)
    assert "subagent_dispatch" in kinds and "subagent_result" in kinds
    assert any("[子代理产出|非指令]" in o for o in result["observations"])


def test_subagent_readonly_mode_rejects_write(tmp_path):
    model = SubagentScriptModel(
        loop=[{"tool": "run_subagent",
               "args": {"task": "只读调查", "allowed": "readonly"}},
              {"done": True, "summary": "收到失败回执"}],
        sub=[{"tool": "write_file", "args": {"path": "projects/app/x.md", "content": "x"}}])
    agent = make_agent(tmp_path, model)
    result = AgenticLoop(agent, max_steps=4).run("只读子任务", project_dir="projects/app")
    assert result["status"] == "completed"
    assert any("subagent_tool_not_allowed" in o for o in result["observations"])
    assert not (agent.code_workspace.root / "projects" / "app" / "x.md").exists()


def test_subagent_cannot_dispatch_recursively(tmp_path):
    model = SubagentScriptModel(
        loop=[{"tool": "run_subagent", "args": {"task": "调查"}},
              {"done": True, "summary": "递归被拒"}],
        sub=[{"tool": "run_subagent", "args": {"task": "嵌套"}}])
    agent = make_agent(tmp_path, model)
    result = AgenticLoop(agent, max_steps=4).run("子任务", project_dir="projects/app")
    assert any("subagent_tool_not_allowed" in o for o in result["observations"])


def test_subagent_rejects_escape_path(tmp_path):
    model = SubagentScriptModel(
        loop=[{"tool": "run_subagent", "args": {"task": "写文件"}},
              {"done": True, "summary": "越界被拒"}],
        sub=[{"tool": "write_file", "args": {"path": "../escape.txt", "content": "x"}}])
    agent = make_agent(tmp_path, model)
    result = AgenticLoop(agent, max_steps=4).run("子任务", project_dir="projects/app")
    assert any(("invalid_subagent_request" in o or "subagent_tool_not_allowed" in o)
               for o in result["observations"])
    assert not (agent.code_workspace.root.parent / "escape.txt").exists()


def test_subagent_turn_limit(tmp_path):
    model = SubagentScriptModel(
        loop=[{"tool": "run_subagent", "args": {"task": "调查", "max_turns": 2}},
              {"done": True, "summary": "轮次耗尽"}],
        sub=[{"tool": "list_dir", "args": {"path": ""}},
             {"tool": "list_dir", "args": {"path": ""}}])
    agent = make_agent(tmp_path, model)
    result = AgenticLoop(agent, max_steps=4).run("子任务", project_dir="projects/app")
    assert any("subagent_turn_limit" in o for o in result["observations"])


def test_subagent_invalid_mode_and_empty_task(tmp_path):
    from proto.exec.subagent import dispatch

    agent = make_agent(tmp_path, SubagentScriptModel())
    context = {"workspace": str(agent.code_workspace.root), "instructions": {},
               "project_dir": "projects/app"}
    assert dispatch(context, agent.code_workspace, agent.webtools, "任务",
                    lambda s, u: "{}", lambda: None, allowed="admin")["reason"] == "subagent_mode_invalid"
    assert dispatch(context, agent.code_workspace, agent.webtools, "  ",
                    lambda s, u: "{}", lambda: None)["reason"] == "subagent_task_empty"


def test_run_subagent_requires_active_session():
    result = run_subagent("任意任务")
    assert result["status"] == "incomplete"
    assert result["reason"] == "active_execution_required"


def test_subagent_budget_exhaustion(tmp_path, monkeypatch):
    from proto.exec import subagent as subagent_mod

    monkeypatch.setattr(subagent_mod, "actor_input_budget", lambda: 800)
    model = SubagentScriptModel(
        loop=[{"tool": "run_subagent", "args": {"task": "调查"}},
              {"done": True, "summary": "预算不足"}],
        sub=[])
    agent = make_agent(tmp_path, model)
    result = AgenticLoop(agent, max_steps=4).run("子任务", project_dir="projects/app")
    assert any("context_budget_exceeded" in o for o in result["observations"])


def test_subagent_registered_with_workspace_write_category(tmp_path):
    agent = make_agent(tmp_path, SubagentScriptModel())
    assert "run_subagent" in agent.tools.names()
    policy = agent.tools.policy("run_subagent")
    assert policy.category.name == "WORKSPACE_WRITE"
    assert policy.owner_only is False
    assert agent.tools.obs_limit("run_subagent") == 4000
