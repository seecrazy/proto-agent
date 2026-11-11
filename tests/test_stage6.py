# -*- coding: utf-8 -*-
"""阶段 6：scratch 工作区隔离 + 审批队列（含日上限）+ 检查点（设计 §8）。"""
import pytest

from proto.kernel.anchor import OwnerAnchor
from proto.kernel.audit import AuditSpine
from proto.kernel.budgets import BudgetPools
from proto.kernel.kernel import ActionCategory, Kernel
from proto.exec.approval import ActionProposal, ApprovalQueue
from proto.exec.checkpoints import CheckpointStore
from proto.exec.workspace import ScratchWorkspace
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
    queue = ApprovalQueue(kernel, spine, daily_cap=3)
    return ws, queue, spine, CheckpointStore(store)


class TestScratchWorkspace:
    def test_write_lands_in_scratch_not_visible(self, env):
        ws, *_ = env
        path = ws.write("drafts/plan.md", "# 草稿")
        assert "scratch" in str(path)
        assert path.read_text(encoding="utf-8") == "# 草稿"
        assert ws.list_visible() == []
        assert ws.list_scratch() == ["drafts/plan.md"]

    def test_path_traversal_rejected(self, env):
        ws, *_ = env
        with pytest.raises(ValueError):
            ws.write("../escape.md", "x")

    def test_promote_requires_granted_approval(self, env):
        ws, queue, spine, _ = env
        ws.write("drafts/plan.md", "# 草稿")
        with pytest.raises(PermissionError):
            ws.promote("drafts/plan.md", approval=None)

    def test_promote_moves_and_audits(self, env):
        ws, queue, spine, _ = env
        ws.write("drafts/plan.md", "# 草稿")
        sub = queue.submit(ActionProposal(ActionCategory.VISIBLE_WRITE, "发布计划草稿",
                                          {"path": "drafts/plan.md"}, task_id="t1"))
        approval = queue.approve(sub.proposal_id)
        ws.promote("drafts/plan.md", approval=approval)
        assert ws.list_visible() == ["drafts/plan.md"]
        assert ws.list_scratch() == []
        assert spine.find(lambda d: d["event_type"] == "promotion")


class TestApprovalQueue:
    def test_read_draft_auto_approved(self, env):
        ws, queue, spine, _ = env
        sub = queue.submit(ActionProposal(ActionCategory.READ_DRAFT, "scratch 内搜索", {}, task_id="t1"))
        assert sub.auto_approved and sub.approval.granted

    def test_visible_write_queues_for_human(self, env):
        ws, queue, spine, _ = env
        sub = queue.submit(ActionProposal(ActionCategory.VISIBLE_WRITE, "改共享 README", {}, task_id="t2"))
        assert not sub.auto_approved
        assert [p.description for p in queue.pending()] == ["改共享 README"]

    def test_irreversible_queues_for_human_and_can_be_denied(self, env):
        ws, queue, spine, _ = env
        sub = queue.submit(ActionProposal(ActionCategory.IRREVERSIBLE, "删除旧数据", {}, task_id="t3"))
        record = queue.deny(sub.proposal_id, reason="不允许删除")
        assert not record.granted
        assert spine.find(lambda d: d["event_type"] == "denial")

    def test_approve_audits(self, env):
        ws, queue, spine, _ = env
        sub = queue.submit(ActionProposal(ActionCategory.VISIBLE_WRITE, "发周报", {}, task_id="t4"))
        queue.approve(sub.proposal_id)
        rows = spine.find(lambda d: d["event_type"] == "approval")
        assert rows and rows[-1]["payload"]["description"] == "发周报"

    def test_daily_cap_blocks_self_initiated(self, env):
        ws, queue, spine, _ = env
        for i in range(3):  # 上限 3
            sub = queue.submit(ActionProposal(ActionCategory.VISIBLE_WRITE, f"自主动作{i}",
                                              {}, task_id=f"t{i}", self_initiated=True))
            assert not sub.auto_approved
        fourth = queue.submit(ActionProposal(ActionCategory.VISIBLE_WRITE, "自主动作3",
                                             {}, task_id="t3b", self_initiated=True))
        assert fourth.rejected and fourth.reason == "daily_cap_exceeded"
        assert spine.find(lambda d: d["event_type"] == "veto" and d["payload"].get("rule") == "daily_cap")

    def test_daily_cap_not_applied_to_owner_initiated(self, env):
        ws, queue, spine, _ = env
        for i in range(5):
            sub = queue.submit(ActionProposal(ActionCategory.VISIBLE_WRITE, f"任务动作{i}",
                                              {}, task_id=f"o{i}"))
            assert not sub.rejected

    def test_mute_blocks_self_initiated_submission(self, env):
        ws, queue, spine, _ = env
        queue.kernel.set_mute(True)
        sub = queue.submit(ActionProposal(ActionCategory.VISIBLE_WRITE, "自主整理",
                                          {}, task_id="m1", self_initiated=True))
        assert sub.rejected and sub.reason == "muted"


class TestCheckpoints:
    def test_roundtrip_with_variant_binding(self, env):
        ws, queue, spine, cps = env
        cps.put("task-9", {"step": 3, "draft": "半成品"}, variant_id="core@1.0")
        rec = cps.resume("task-9")
        assert rec["state"]["step"] == 3
        assert rec["variant_id"] == "core@1.0"

    def test_resume_missing_returns_none(self, env):
        *_, cps = env
        assert cps.resume("nope") is None
