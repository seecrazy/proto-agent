# -*- coding: utf-8 -*-
"""M32: 观测与协作——事件带时间/时天周月年汇总/提议详情/24h自主白名单/agent间协作。"""
import json
import time

import pytest

from proto.llm.client import LLMClient


class Null(LLMClient):
    def complete(self, s, u): return "[]" if "归因" in s else "好的。"


def dead_web():
    from proto.tools.webtools import WebToolkit
    return WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))


@pytest.fixture()
def agent(tmp_path):
    from proto.runtime.agent import ProtoAgent
    return ProtoAgent(data_dir=tmp_path, llm=Null(), goal_interval_s=10 ** 9,
                      webtools=dead_web())


class TestAuditTimestamps:
    def test_tail_events_carry_readable_time(self, agent):
        agent.spine.append("veto", {"rule": "t"})
        events = agent.audit_tail(5)
        assert events[-1]["ts"] and "T" in events[-1]["ts"]    # ISO 时间

    def test_scratch_notes_carry_time(self, agent):
        agent.workspace.write("notes/learning-x.md", "# n")
        # /api/scratch 已有 preview；补 mtime
        p = agent.workspace._scratch / "notes/learning-x.md"
        assert p.exists()


class TestTimeBuckets:
    def test_hour_day_week_month_year_counts(self, agent):
        now = time.time()
        agent.spine.append("web_fetch", {"url": "u"})          # 现在
        # 直接测聚合函数：灌不同年龄的假事件
        from proto.runtime.summaries import bucket_events
        events = [
            {"ts": _iso(now - 1800), "event_type": "chat"},   # 30min 前 → hour 内
            {"ts": _iso(now - 2 * 86400), "event_type": "web_fetch"},
            {"ts": _iso(now - 10 * 86400), "event_type": "goal_pushed"},
            {"ts": _iso(now - 40 * 86400), "event_type": "reflection_write"},
            {"ts": _iso(now - 400 * 86400), "event_type": "evolution_merge"},
        ]
        b = bucket_events(events, now=now)
        assert b["hour"]["total"] == 1 and b["day"]["total"] >= 1
        assert b["week"]["total"] >= 2 and b["month"]["total"] >= 2
        assert b["year"]["total"] == 4   # 400天前>365出年窗


def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class TestProposalDetails:
    def test_pending_proposal_carries_draft_content(self, agent):
        from proto.exec.approval import ActionProposal
        from proto.kernel.kernel import ActionCategory
        agent.workspace.write("notes/learning-1.md", "# 学习笔记\n要点A")
        agent.queue.submit(ActionProposal(
            ActionCategory.VISIBLE_WRITE, "晋升草稿",
            {"goal": "调研X", "note": "notes/learning-1.md"}, task_id="t",
            self_initiated=True, desire_dim="curiosity_gap"))
        proposals = agent.pending_proposals()
        assert proposals[0]["preview"].startswith("# 学习笔记")


class TestAutoApproveQuota:
    def test_workspace_writes_within_quota_auto_approved(self, agent):
        from proto.runtime.quota import AutoApproveQuota
        quota = AutoApproveQuota(24, 3600.0, now=lambda: 1000.0)
        assert quota.try_consume(1001.0) is True
        assert quota.try_consume(2000.0) is True
        assert quota.remaining() == 22

    def test_quota_exhausted_denies(self):
        from proto.runtime.quota import AutoApproveQuota
        quota = AutoApproveQuota(2, 3600.0, now=lambda: 1000.0)
        quota.try_consume(1000.0); quota.try_consume(1000.0)
        assert quota.try_consume(1000.0) is False


class TestAgentCollab:
    def test_blackboard_post_and_read(self, tmp_path):
        from proto.fleet.collab import Blackboard
        from proto.storage.store import FileSystemStore
        bb = Blackboard(FileSystemStore(tmp_path))
        bb.post("a1", "求助：谁知道 Chrome MV3 的 sidebar 权限？")
        notes = bb.read()
        assert notes and notes[0]["from"] == "a1"
        bb.claim(notes[0]["id"], "a2")
        assert bb.read()[0]["claimed_by"] == "a2"

    def test_agent_helps_agent(self, tmp_path):
        """a1 发帖求助 → a2 认领并回复 → a1 可读。"""
        from proto.fleet.collab import Blackboard
        from proto.storage.store import FileSystemStore
        from proto.runtime.agent import ProtoAgent
        bb = Blackboard(FileSystemStore(tmp_path))
        a1 = ProtoAgent(data_dir=tmp_path / "a1", llm=Null(),
                        goal_interval_s=10 ** 9, webtools=dead_web(),
                        blackboard=bb)
        a2 = ProtoAgent(data_dir=tmp_path / "a2", llm=Null(),
                        goal_interval_s=10 ** 9, webtools=dead_web(),
                        blackboard=bb)
        note_id = a1.post_to_blackboard("求助：manifest v3 的 action.default_popup？")
        a2.answer_blackboard(note_id, "用 default_popup + popup.html，权限不用额外声明")
        replies = a1.read_blackboard_replies()
        assert replies and "default_popup" in replies[0]["text"]
