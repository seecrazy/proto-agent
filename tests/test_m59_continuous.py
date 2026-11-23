# -*- coding: utf-8 -*-
"""M59：连续工作门控——用"意义"替代"冷却"（用户："没做完又不累，为什么休息；
应识别无意义和错误并调整，而不是无缘无故停下等冷却"）。

新门控语义：
- 有活+预算+未暂停 → 每拍心跳都干（无冷却）
- 停止只因真实原因：预算地板 / 无目标 / 全部目标连续失败（要换思路或问主人）
  / 链瞬间崩溃保护（llm_error 秒败重试节流，防打转烧钱）
- 跨轮失败反馈：上一轮验证缺什么，下一轮开局就知道（换打法，不是重试同招）
- 假勤奋识别：write_file 写入与现有内容完全相同 = 无产出，计入零产出升级
"""
import json

import pytest

from proto.exec.agentic import AgenticLoop


class LoopLLM:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.prompts = []

    def complete(self, system, user):
        self.prompts.append((system, user))
        item = self.decisions.pop(0)
        return item if isinstance(item, str) else json.dumps(item)


@pytest.fixture()
def agent(tmp_path):
    from proto.tools.webtools import WebToolkit
    from proto.runtime.agent import ProtoAgent
    dead = WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))
    return ProtoAgent(data_dir=tmp_path, llm=LoopLLM([]), goal_interval_s=10**9,
                      webtools=dead)


class TestChainGate:
    def test_productive_round_unlocks_immediately(self, agent):
        """干完一轮（completed/verified）→ 立即允许下一轮（原 30 分钟冷却不再挡）。"""
        agent._last_chain = {"ended": agent._now() - 5, "status": "verified"}
        assert agent._may_start_chain() is True

    def test_crash_loop_throttled(self, agent):
        """秒败（llm_error/no_llm）15 秒内不重试——防打转烧钱，这不是休息是熔断。"""
        agent._last_chain = {"ended": agent._now() - 5, "status": "failed"}
        assert agent._may_start_chain() is False
        agent._last_chain = {"ended": agent._now() - 120, "status": "failed"}
        assert agent._may_start_chain() is True

    def test_failed_verification_retries_immediately(self, agent):
        """验证失败但链真跑过 → 立即允许重试（换打法靠跨轮反馈，不靠冷却）。"""
        agent._last_chain = {"ended": agent._now() - 5,
                             "status": "failed_verification"}
        assert agent._may_start_chain() is True

    def test_legacy_failure_history_is_not_automatically_reset(self, agent):
        goal = agent.goal_tree.add("旧目标", "mid")
        goal.fail_streak = 5
        assert agent._may_start_chain() is True
        assert goal.fail_streak == 5
        assert not agent.spine.find(lambda event: event["event_type"] == "approach_reset")


class TestCrossRoundFeedback:
    def test_previous_failure_feeds_next_round(self, agent, tmp_path):
        """上轮验证缺 X → 本轮任务开局注入"缺 X 必须补齐"（跨轮反馈）。"""
        from proto.goals.system import GoalNode
        from proto.work.journal import GoalJournal
        agent.goal_tree.add("实现功能", "mid")
        node = agent.goal_tree.all_active()[0]
        j = GoalJournal(agent.data_dir / "work" / node.id / "JOURNAL.md")
        j.note_round("awaiting_retry", ["结构已明"],
                     ["projects/app/a.swift"], ["补齐 CRUD 测试覆盖"])
        agent.llm.decisions = [
            '{"acceptance": "文件存在"}',           # generate_acceptance
            "plan",                                  # loop plan
            {"done": True, "summary": "ok"},         # loop 首步即 done
            "发现：ok\n产出：a\n下一步：done",       # journal summarize
        ]
        agent.work_on_goal(agent.goal_tree.all_active()[0])
        all_prompts = " ".join(u for _, u in agent.llm.prompts)
        assert "CRUD 测试覆盖" in all_prompts       # 上轮缺口进了本轮开局


class TestNoOpWriteDetection:
    def test_identical_write_counts_as_no_output(self, agent, tmp_path):
        """写同样内容=没干活：journal 产出排除 unchanged，零产出升级计数。"""
        root = agent.code_workspace.root
        p = root / "projects" / "x" / "a.swift"
        p.parent.mkdir(parents=True)
        p.write_text("let a = 1\n", encoding="utf-8")
        from proto.work.journal import GoalJournal
        j = GoalJournal(tmp_path / "J.md")
        trace = [{"tool": "write_file",
                  "args": {"path": "projects/x/a.swift", "content": "let a = 1\n"},
                  "result": "unchanged"}]
        produced = [f for f in GoalJournal._written_files(trace)
                    if not str(f).endswith("unchanged")]
        # _written_files 按结果过滤 unchanged
        assert GoalJournal._written_files(trace) == []

    def test_write_file_reports_unchanged(self, agent):
        root = agent.code_workspace.root
        f = root / "a.txt"
        f.write_text("hello", encoding="utf-8")
        r1 = agent.code_workspace.write_file("a.txt", "hello")
        r2 = agent.code_workspace.write_file("a.txt", "world")
        assert "unchanged" in str(r1)
        assert "written" in str(r2)
