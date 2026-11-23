# -*- coding: utf-8 -*-
"""M38: 主人超时自主裁决——低风险等不到回复可自行批准继续；
高危（IRREVERSIBLE）与高权限（owner_only 工具）永远只等主人。"""
import json

import pytest

from proto.exec.router import Task
from proto.tools.webtools import WebToolkit
from proto.kernel.kernel import ActionCategory
from proto.llm.client import LLMClient
from proto.runtime.agent import ProtoAgent


class Null(LLMClient):
    def complete(self, system, user):
        return "（离线）"


class RiskLow(LLMClient):
    def complete(self, system, user):
        if "自主裁决器" in system:
            return '{"risk": "low", "reason": "本地文件，可回滚"}'
        return "好的。"


def make_agent(tmp_path, llm=None, **env):
    import os
    for k, v in env.items():
        os.environ[k] = v
    try:
        return ProtoAgent(data_dir=tmp_path / "d", llm=llm or Null(),
                          goal_interval_s=10 ** 9,
                          webtools=WebToolkit(
                              fetch=lambda u, x="t": (_ for _ in ()).throw(OSError("禁网"))))
    finally:
        for k in env:
            os.environ.pop(k, None)


def age_pending(agent, seconds=3600):
    for p in agent.queue._pending.values():
        p.submitted_at -= seconds


def paused_write_task(agent, task_id="t-timeout"):
    result = agent.router.run(Task(id=task_id, description="产出报告", steps=[
        {"tool": "write_file", "args": {"path": "report.md", "content": "hi"},
         "category": "visible_write"}]))
    assert result.status == "paused"
    return result.proposal_id


class TestTimeoutAutoAdjudication:
    def test_low_risk_promotion_auto_approved_after_timeout(self, tmp_path):
        agent = make_agent(tmp_path)
        pid = paused_write_task(agent)
        age_pending(agent)
        agent._maybe_timeout_decide()
        assert agent.queue.pending() == []                 # 已自行批准
        assert agent.workspace.list_visible() == ["report.md"]   # 断点续跑完成
        assert agent.spine.find(lambda d: d["event_type"] == "owner_timeout_auto")
        assert agent.timeout_quota.remaining() == 2        # 默认 3 次/天，用 1
        assert any("自主裁决" in m for m in agent._inbox)

    def test_fresh_proposal_untouched(self, tmp_path):
        agent = make_agent(tmp_path)
        paused_write_task(agent)
        agent._maybe_timeout_decide()                      # 未到超时
        assert len(agent.queue.pending()) == 1
        assert not agent.spine.find(lambda d: d["event_type"] == "owner_timeout_auto")

    def test_irreversible_never_auto(self, tmp_path):
        agent = make_agent(tmp_path)
        from proto.exec.approval import ActionProposal
        sub = agent.queue.submit(ActionProposal(
            ActionCategory.IRREVERSIBLE, "删除生产数据", {"x": 1},
            task_id="t-irr", self_initiated=True))
        assert not sub.auto_approved
        age_pending(agent)
        agent._maybe_timeout_decide()
        agent._maybe_timeout_decide()                      # 第二拍：不重复提醒
        keeps = agent.spine.find(lambda d: d["event_type"] == "owner_timeout_keep")
        assert len(keeps) == 1                             # 每提议只裁一次
        assert "irreversible" in keeps[0]["payload"]["reason"]
        assert len(agent.queue.pending()) == 1             # 继续等主人

    def test_owner_only_tool_never_auto(self, tmp_path):
        agent = make_agent(tmp_path, llm=RiskLow())
        agent.tools.register("elevated_thing", lambda: "x",
                             category=ActionCategory.VISIBLE_WRITE,
                             description="高权限动作", owner_only=True)
        from proto.exec.approval import ActionProposal
        agent.queue.submit(ActionProposal(
            ActionCategory.VISIBLE_WRITE, "外部执行某事",
            {"step": {"tool": "elevated_thing", "args": {}}}, task_id="t-el",
            self_initiated=True))
        age_pending(agent)
        agent._maybe_timeout_decide()
        keep = agent.spine.find(lambda d: d["event_type"] == "owner_timeout_keep")
        assert "elevated_permission" in keep[0]["payload"]["reason"]
        assert len(agent.queue.pending()) == 1

    def test_unknown_tool_keeps_waiting(self, tmp_path):
        agent = make_agent(tmp_path)
        from proto.exec.approval import ActionProposal
        agent.queue.submit(ActionProposal(
            ActionCategory.VISIBLE_WRITE, "神秘动作",
            {"step": {"tool": "mystery", "args": {}}}, task_id="t-x",
            self_initiated=True))
        age_pending(agent)
        agent._maybe_timeout_decide()
        assert agent.spine.find(lambda d: d["event_type"] == "owner_timeout_keep")

    def test_generic_tool_llm_low_risk_auto(self, tmp_path):
        agent = make_agent(tmp_path, llm=RiskLow())
        agent.tools.register("publish_note", lambda path: "ok",
                             category=ActionCategory.VISIBLE_WRITE,
                             description="发布笔记")
        from proto.exec.approval import ActionProposal
        agent.queue.submit(ActionProposal(
            ActionCategory.VISIBLE_WRITE, "发布学习笔记",
            {"step": {"tool": "publish_note", "args": {"path": "n.md"}}},
            task_id="t-pub", self_initiated=True))
        age_pending(agent)
        agent._maybe_timeout_decide()
        auto = agent.spine.find(lambda d: d["event_type"] == "owner_timeout_auto")
        assert auto and "risk_low" in auto[0]["payload"]["reason"]

    def test_generic_tool_llm_high_risk_keeps(self, tmp_path):
        class RiskHigh(RiskLow):
            def complete(self, system, user):
                if "自主裁决器" in system:
                    return '{"risk": "high", "reason": "对外发布不可撤回"}'
                return "好的。"
        agent = make_agent(tmp_path, llm=RiskHigh())
        agent.tools.register("publish_note", lambda path: "ok",
                             category=ActionCategory.VISIBLE_WRITE,
                             description="发布笔记")
        from proto.exec.approval import ActionProposal
        agent.queue.submit(ActionProposal(
            ActionCategory.VISIBLE_WRITE, "发布公告",
            {"step": {"tool": "publish_note", "args": {}}}, task_id="t-p2",
            self_initiated=True))
        age_pending(agent)
        agent._maybe_timeout_decide()
        keep = agent.spine.find(lambda d: d["event_type"] == "owner_timeout_keep")
        assert "risk_high" in keep[0]["payload"]["reason"]

    def test_offline_generic_tool_conservative(self, tmp_path):
        agent = make_agent(tmp_path)                        # Null LLM：评估不可达
        agent.tools.register("publish_note", lambda path: "ok",
                             category=ActionCategory.VISIBLE_WRITE,
                             description="发布笔记")
        from proto.exec.approval import ActionProposal
        agent.queue.submit(ActionProposal(
            ActionCategory.VISIBLE_WRITE, "发布公告",
            {"step": {"tool": "publish_note", "args": {}}}, task_id="t-p3",
            self_initiated=True))
        age_pending(agent)
        agent._maybe_timeout_decide()
        keep = agent.spine.find(lambda d: d["event_type"] == "owner_timeout_keep")
        assert "risk_assess_failed" in keep[0]["payload"]["reason"]

    def test_quota_exhaustion_keeps_waiting(self, tmp_path):
        agent = make_agent(tmp_path, PROTO_TIMEOUT_AUTO_N="1")
        paused_write_task(agent, "t-q1")
        result2 = agent.router.run(Task(id="t-q2", description="产出二", steps=[
            {"tool": "write_file", "args": {"path": "r2.md", "content": "x"},
             "category": "visible_write"}]))
        age_pending(agent)
        agent._maybe_timeout_decide()
        assert agent.spine.find(lambda d: d["event_type"] == "owner_timeout_auto")
        keeps = agent.spine.find(lambda d: d["event_type"] == "owner_timeout_keep")
        assert any("quota_exhausted" in k["payload"]["reason"] for k in keeps)
        assert len(agent.queue.pending()) == 1              # 第二个继续等

    def test_timeout_quota_survives_restart(self, tmp_path):
        a1 = make_agent(tmp_path, PROTO_TIMEOUT_AUTO_N="2")
        assert a1.timeout_quota.try_consume()
        a2 = make_agent(tmp_path, PROTO_TIMEOUT_AUTO_N="2")
        assert a2.timeout_quota.remaining() == 1            # 重启不重置额度
