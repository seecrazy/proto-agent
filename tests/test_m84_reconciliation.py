# -*- coding: utf-8 -*-
"""M84：结果未知，待核对——谁来核对、怎么核对。

生产实证（2026-11-24 21:40）：主人在 run 执行中途重启控制台，该 run 进入
needs_reconciliation / interrupted_without_final_receipt，界面显示「结果未知，待核对」，
然后就没有然后了——谁核对、怎么核对，没有任何说明，也没有自动化。

设计立场（D7 的落地）：效果可以从本机磁盘验证的步骤（工作区内的 write_file /
edit_file），由 PROTO 自己核对——文件在不在、内容对不对就是真相；只有对外部
世界有不可逆影响的动作（visible_write、审批中的提议）才留给主人。
"""
import json

import pytest

from proto.exec.agentic import LOOP_SYSTEM, AgenticLoop
from proto.fleet import runs as dispatcher
from proto.fleet.manager import AgentManager
from proto.runtime.agent import ProtoAgent
from tests.test_m71_continuity import DONE, WRITE, Model, member, start_autonomous, step


def interrupted_session(agent, pending):
    """把 agent 当前会话伪装成「进程在第 pending 步执行中途死掉」。"""
    session = agent.sessions.lookup(agent._last_chain_session) if hasattr(agent, "_last_chain_session") else None
    raise NotImplementedError


@pytest.mark.parametrize("on_disk,expected", [
    # write_file：内容已在磁盘 → 该步已生效
    ("content", "landed"),
    # write_file：文件不存在 → 该步根本没执行
    (None, "not_landed"),
])
def test_interrupted_workspace_write_is_reconciled_from_disk(tmp_path, monkeypatch, on_disk, expected):
    write = {"tool": "write_file", "args": {"path": "Report.md", "content": "产品名称"}, "category": "workspace_write"}
    model = Model([write, DONE])
    fleet, agent = member(tmp_path, model)
    if on_disk == "content":
        agent.code_workspace.write_file("Report.md", "产品名称")
    run, _ = start_autonomous(fleet, agent)
    finished = step(fleet)
    if on_disk is None:
        (agent.code_workspace.root / "Report.md").unlink()      # 模拟写入根本没发生
    session = agent.sessions.lookup(finished["session_id"])
    # 伪造：进程在第 9 步执行中途死掉——run 仍是 running、会话挂着一个没有完成回执的 pending write
    session.update(status="running", pending_decision=None,
                   pending_call={"task_id": session["id"] + "-9", "step": 9, "tool": "write_file",
                                 "args": {"path": "Report.md", "content": "产品名称"},
                                 "category": "workspace_write", "note": "写报告"})
    agent.sessions.save(session)
    with fleet.runs.database.connect(write=True) as connection:
        connection.execute("UPDATE runs SET status='running', result=NULL WHERE id=?", (run["id"],))
    fleet.runs._recover_run(fleet.runs.get(run["id"]), agent, startup=True)
    verdict = fleet.runs.get(run["id"])
    if expected == "landed":
        assert verdict["status"] == "queued" and verdict["reason"] == "interrupted_requeued"
        resumed = agent.sessions.lookup(session["id"])
        assert any("Report.md" in text and "写入已落盘" in text for text in resumed["observations"])
        assert resumed["pending_call"] is None
    else:
        assert verdict["status"] == "queued" and verdict["reason"] == "interrupted_requeued"
        resumed = agent.sessions.lookup(session["id"])
        assert any("Report.md" in text and "写入未落盘" in text for text in resumed["observations"])


def test_interrupted_edit_file_verdict_covers_all_three_cases(tmp_path):
    from proto.fleet.runs import workspace_effect_verdict
    tools = agent_with_workspace(tmp_path)
    landed = {"task_id": "t", "step": 0, "tool": "edit_file",
              "args": {"path": "a.md", "old": "旧文本", "new": "新文本"}, "category": "workspace_write"}
    tools.write_file("a.md", "新文本\n")
    assert workspace_effect_verdict(tools, landed) == "landed"
    tools.write_file("a.md", "旧文本\n")
    assert workspace_effect_verdict(tools, landed) == "not_landed"
    tools.write_file("a.md", "完全不相关\n")
    assert workspace_effect_verdict(tools, landed) is None            # 说不清 → 仍留给主人


def test_visible_write_and_unknown_tools_stay_with_the_owner(tmp_path):
    from proto.fleet.runs import workspace_effect_verdict
    tools = agent_with_workspace(tmp_path)
    visible = {"task_id": "t", "step": 0, "tool": "run_command",
               "args": {"argv": ["python3", "send.py"]}, "category": "visible_write"}
    assert workspace_effect_verdict(tools, visible) is None
    command = {"task_id": "t", "step": 0, "tool": "run_command",
               "args": {"argv": ["python3", "-c", "print(1)"]}, "category": "workspace_write"}
    assert workspace_effect_verdict(tools, command) is None          # 命令的外部效果说不清，不自动判


def agent_with_workspace(tmp_path):
    agent = ProtoAgent(tmp_path / "ag", goal_interval_s=0)
    return agent.code_workspace
