# -*- coding: utf-8 -*-
"""M8-B(P1): 暂停任务可从检查点恢复（API + 运行时）。"""
import json

import pytest

from proto.llm.client import LLMClient
from proto.runtime.agent import ProtoAgent


class ScriptedLLM(LLMClient):
    def __init__(self, replies=None):
        self.replies = list(replies or [])
    def complete(self, system, user):
        if "归因" in system:
            return "[]"
        return self.replies.pop(0) if self.replies else "好的。"


VISIBLE_TASK = [{"tool": "write_file",
                 "args": {"path": "report.md", "content": "# 报告"},
                 "category": "visible_write"}]


def test_router_checkpoint_carries_task_spec(tmp_path):
    from proto.exec.approval import ApprovalQueue
    from proto.exec.checkpoints import CheckpointStore
    from proto.exec.router import Router, Task, ToolRegistry
    from proto.exec.workspace import ScratchWorkspace
    from proto.kernel.anchor import OwnerAnchor
    from proto.kernel.audit import AuditSpine
    from proto.kernel.budgets import BudgetPools
    from proto.kernel.kernel import Kernel
    from proto.state.store import FileSystemStore

    store = FileSystemStore(tmp_path / "d")
    spine = AuditSpine(store)
    kernel = Kernel(OwnerAnchor.enroll(store, spine, b"\x01" * 32), spine,
                    BudgetPools({"global": {"tokens": 1000}, "owner_tasks": {"tokens": 800},
                                 "self_initiated": {"tokens": 200}}))
    queue = ApprovalQueue(kernel, spine)
    cps = CheckpointStore(store)
    router = Router(kernel, queue, ScratchWorkspace(tmp_path / "w", spine), cps,
                    ToolRegistry())
    result = router.run(Task(id="t1", description="写报告", steps=VISIBLE_TASK))
    assert result.status == "paused"
    record = cps.resume("t1")
    assert record["state"]["task"]["description"] == "写报告"
    assert record["state"]["task"]["steps"] == VISIBLE_TASK


def test_agent_resume_task_approved_completes(tmp_path):
    agent = ProtoAgent(data_dir=tmp_path, llm=ScriptedLLM(),
                       goal_interval_s=10 ** 9)
    result = agent.run_task_with_steps("写报告", VISIBLE_TASK)
    assert result["status"] == "paused"
    final = agent.resume_task(result["task_id"], approved=True)
    assert final["status"] == "completed"


def test_agent_resume_task_denied_keeps_draft_in_scratch(tmp_path):
    agent = ProtoAgent(data_dir=tmp_path, llm=ScriptedLLM(),
                       goal_interval_s=10 ** 9)
    result = agent.run_task_with_steps("写报告", VISIBLE_TASK)
    final = agent.resume_task(result["task_id"], approved=False)
    assert final["status"] == "denied"
    assert agent.workspace.list_scratch() == ["report.md"]
