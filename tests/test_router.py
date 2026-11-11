# -*- coding: utf-8 -*-
"""M4-②: Router/Worker 执行器骨架——任务队列、工具注册、逐步检查点、审批暂停/恢复（设计 §8）。"""
import pytest

from proto.exec.approval import ApprovalQueue
from proto.exec.checkpoints import CheckpointStore
from proto.exec.router import Router, Task, ToolRegistry
from proto.exec.workspace import ScratchWorkspace
from proto.kernel.anchor import OwnerAnchor
from proto.kernel.audit import AuditSpine
from proto.kernel.budgets import BudgetPools
from proto.kernel.kernel import ActionCategory, Kernel
from proto.state.store import FileSystemStore


@pytest.fixture()
def env(tmp_path):
    store = FileSystemStore(tmp_path / "data")
    spine = AuditSpine(store)
    anchor = OwnerAnchor.enroll(store, spine, b"\x01" * 32)
    pools = BudgetPools({"global": {"tokens": 1000.0}, "owner_tasks": {"tokens": 800.0},
                         "self_initiated": {"tokens": 200.0}})
    kernel = Kernel(anchor, spine, pools)
    ws = ScratchWorkspace(tmp_path / "ws", audit=spine)
    queue = ApprovalQueue(kernel, spine)
    cps = CheckpointStore(store)
    tools = ToolRegistry()
    tools.register("search", lambda q: f"结果:{q}")
    tools.register("summarize", lambda text: f"摘要({len(text)}字)")
    router = Router(kernel, queue, ws, cps, tools)
    return router, queue, cps, ws, spine, kernel


def readonly_task():
    return Task(id="t1", description="调研并总结", steps=[
        {"tool": "search", "args": {"q": "agent 安全"}, "category": "read_draft"},
        {"tool": "summarize", "args": {"text": "abc"}, "category": "read_draft"},
    ])


class TestRouter:
    def test_readonly_task_completes_without_approval(self, env):
        router, queue, cps, ws, spine, kernel = env
        result = router.run(readonly_task())
        assert result.status == "completed"
        assert len(result.step_results) == 2
        assert "结果:agent 安全" in result.step_results[0]
        assert queue.pending() == []
        assert cps.resume("t1")["state"]["step"] == 2

    def test_owner_task_consumes_owner_pool(self, env):
        router, *_ , kernel = env
        router.run(readonly_task())
        assert kernel.pools.remaining("owner_tasks", "tokens") == 800.0 - 40.0

    def test_self_initiated_task_consumes_self_pool(self, env):
        router, *_ , kernel = env
        task = readonly_task()
        task.owner_assigned = False
        task.id = "t2"
        result = router.run(task)
        assert result.status == "completed"
        assert kernel.pools.remaining("self_initiated", "tokens") == 200.0 - 40.0

    def test_no_budget_task_not_started(self, env):
        router, *_, kernel = env
        kernel.pools.try_consume("self_initiated", {"tokens": 200})
        task = readonly_task()
        task.owner_assigned = False
        task.id = "t3"
        result = router.run(task)
        assert result.status == "no_budget"
        assert result.step_results == []

    def test_visible_write_pauses_until_approval(self, env):
        router, queue, cps, ws, spine, kernel = env
        task = Task(id="t4", description="产出周报", steps=[
            {"tool": "write_file", "args": {"path": "weekly.md", "content": "# 周报"},
             "category": "visible_write"},
        ])
        result = router.run(task)
        assert result.status == "paused"
        assert "周报" in queue.pending()[0].description
        assert ws.list_scratch() == ["weekly.md"]      # 暂停在 scratch，未晋升
        assert ws.list_visible() == []
        # 人类批准 → 恢复执行完成
        pid = result.proposal_id
        final = router.resume(task, human_approved=True)
        assert final.status == "completed"
        assert ws.list_visible() == ["weekly.md"]      # 批准后晋升
        assert ws.list_scratch() == []

    def test_denied_visible_write_aborts(self, env):
        router, queue, cps, ws, spine, kernel = env
        task = Task(id="t5", description="产出周报", steps=[
            {"tool": "search", "args": {"q": "x"}, "category": "read_draft"},
            {"tool": "write_file", "args": {"path": "w.md", "content": "x"},
             "category": "visible_write"},
        ])
        result = router.run(task)
        assert result.status == "paused"
        final = router.resume(task, human_approved=False)
        assert final.status == "denied"
        assert ws.list_scratch() == ["w.md"]           # 拒绝：留在 scratch，不晋升
        assert ws.list_visible() == []

    def test_unknown_tool_fails_task(self, env):
        router, *_ = env
        task = Task(id="t6", description="坏任务", steps=[
            {"tool": "nope", "args": {}, "category": "read_draft"},
        ])
        result = router.run(task)
        assert result.status == "failed"
        assert "unknown_tool" in result.reason
