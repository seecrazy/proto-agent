# -*- coding: utf-8 -*-
"""M45：接力工作记忆 + 步数经济学——自主做事像 ZCode 一样有推进力。

两晚生产证据：84 步重写文档 / 6×10 步 list_dir 游荡零写入。
根因：尝试间失忆（每 30 分钟从零探索）+ 步数配额与探索/产出失衡。
修复：接力日志跨尝试传递（发现/产出/下一步）、探索配额强制产出、
零产出升级、list_dir 降本（跳过 _archived）、预算地板不起必死链。
"""
import json

import pytest

from proto.exec.agentic import AgenticLoop
from proto.work.journal import GoalJournal


class LoopLLM:
    """脚本化决策桩（与 test_m16 同款，附 prompts 记录）。"""
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.prompts = []

    def complete(self, system, user):
        self.prompts.append((system, user))
        item = self.decisions.pop(0)
        return item if isinstance(item, str) else json.dumps(item)


@pytest.fixture()
def agent(tmp_path):
    from proto.exec.webtools import WebToolkit
    from proto.runtime.agent import ProtoAgent
    dead = WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))
    return ProtoAgent(data_dir=tmp_path, llm=LoopLLM([]), goal_interval_s=10**9,
                      webtools=dead)


class TestGoalJournal:
    def test_round_trip_and_resume_head(self, tmp_path):
        j = GoalJournal(tmp_path / "J.md")
        assert j.resume_head() == ""          # 空日志开局干净
        j.note_round("max_steps",
                     findings=["设计规格在 projects/A/", "数据模型已定"],
                     produced=["projects/A/DESIGN.md"],
                     next_steps=["写 App.swift 入口"])
        j.note_round("verified",
                     findings=["入口写完"],
                     produced=["projects/A/App.swift"],
                     next_steps=["补单测"])
        head = j.resume_head()
        assert "写 App.swift 入口" in head or "补单测" in head
        assert "projects/A/DESIGN.md" in head

    def test_consecutive_no_output(self, tmp_path):
        j = GoalJournal(tmp_path / "J.md")
        assert j.consecutive_no_output() == 0
        j.note_round("max_steps", ["探索了"], ["projects/a.py"], ["继续"])
        assert j.consecutive_no_output() == 0
        j.note_round("max_steps", ["又探索了"], [], ["还探索"])
        assert j.consecutive_no_output() == 1
        j.note_round("max_steps", ["还在探索"], [], ["再探索"])
        assert j.consecutive_no_output() == 2
        j.note_round("verified", ["产出"], ["projects/b.py"], ["收尾"])
        assert j.consecutive_no_output() == 0

    def test_finish_mechanical_extracts_writes(self, tmp_path):
        j = GoalJournal(tmp_path / "J.md")
        trace = [
            {"tool": "list_dir", "args": {}, "result": "a/ b/"},
            {"tool": "write_file", "args": {"path": "projects/x/a.swift"},
             "result": "ok"},
            {"tool": "run_python", "args": {"code": "pass"}, "result": ""},
        ]
        j.finish("max_steps", ["list_dir → a/ b/"], trace, summarize=None)
        head = j.resume_head()
        assert "projects/x/a.swift" in head

    def test_finish_with_llm_summary(self, tmp_path):
        j = GoalJournal(tmp_path / "J.md")
        def fake_summarize(prompt, text):
            return "发现：布局已摸清\n产出：无\n下一步：直接写 ContentView.swift"
        j.finish("max_steps", ["grep → hit"], [], summarize=fake_summarize)
        assert "ContentView.swift" in j.resume_head()


class TestLoopMomentum:
    def test_resume_block_injected(self, agent, tmp_path):
        j = GoalJournal(tmp_path / "J.md")
        j.note_round("max_steps", ["规格已齐"], ["projects/A/SPEC.md"],
                     ["写 App.swift"])
        agent.llm.decisions = [
            "直接续写",                                  # plan 调用（llm_fast=llm）
            {"done": True, "summary": "ok"},
            "发现：ok\n产出：无\n下一步：done",           # journal summarize 调用
        ]
        AgenticLoop(agent, max_steps=3).run("实现 App", journal=j)
        loop_prompt = agent.llm.prompts[1][1]          # prompts[0] 是 plan
        assert "上一轮" in loop_prompt and "写 App.swift" in loop_prompt
        assert "不要重新" in loop_prompt or "接着" in loop_prompt

    def test_explore_quota_forces_output(self, agent):
        """连续 4 步只读探索 → 注入强制产出提示，下一轮模型必须看见。"""
        agent.llm.decisions = [
            "先看看",                                    # plan
            {"tool": "list_dir", "args": {}, "category": "read_draft"},
            {"tool": "list_dir", "args": {"path": "projects"}, "category": "read_draft"},
            {"tool": "read_file", "args": {"path": "README.md"}, "category": "read_draft"},
            {"tool": "grep", "args": {"pattern": "App"}, "category": "read_draft"},
            {"tool": "list_dir", "args": {}, "category": "read_draft"},
            {"done": True, "summary": "被迫收工"},
            "发现：x\n产出：无\n下一步：写文件",          # summarize
        ]
        result = AgenticLoop(agent, max_steps=8).run("实现某功能")
        forced = [o for o in result["observations"] if "必须" in o and "write_file" in o]
        assert forced, "连续探索未触发强制产出提示"
        later_prompts = [p[1] for p in agent.llm.prompts[1:]]
        assert any("禁止再" in p for p in later_prompts)

    def test_write_tool_resets_explore_counter(self, agent):
        agent.llm.decisions = [
            "干",                                        # plan
            {"tool": "list_dir", "args": {}, "category": "read_draft"},
            {"tool": "list_dir", "args": {}, "category": "read_draft"},
            {"tool": "write_file",
             "args": {"path": "a.py", "content": "x=1"}, "category": "workspace_write"},
            {"tool": "list_dir", "args": {}, "category": "read_draft"},
            {"tool": "list_dir", "args": {}, "category": "read_draft"},
            {"done": True, "summary": "ok"},
            "发现：ok\n产出：a.py\n下一步：done",         # summarize
        ]
        result = AgenticLoop(agent, max_steps=8).run("t")
        assert not [o for o in result["observations"] if "禁止再" in o]

    def test_journal_records_round_on_finish(self, agent, tmp_path):
        j = GoalJournal(tmp_path / "J.md")
        agent.llm.decisions = [
            "写 b.py",                                   # plan
            {"tool": "write_file",
             "args": {"path": "b.py", "content": "y=2"}, "category": "workspace_write"},
            {"done": True, "summary": "写了 b.py"},
            "发现：完成\n产出：b.py\n下一步：验证",        # summarize
        ]
        AgenticLoop(agent, max_steps=4).run("写文件", journal=j)
        assert "b.py" in j.resume_head()


class TestListDirCost:
    def test_archived_excluded_from_listing(self, agent, tmp_path):
        root = agent.code_workspace.root
        (root / "projects" / "real").mkdir(parents=True)
        (root / "projects" / "real" / "main.swift").write_text("x", encoding="utf-8")
        (root / "projects" / "_archived" / "junk").mkdir(parents=True)
        (root / "projects" / "_archived" / "junk" / "old.md").write_text(
            "x", encoding="utf-8")
        listing = agent.code_workspace.list_dir("")
        assert "real/main.swift" in listing
        assert "_archived" not in listing

    def test_listing_capped_with_hint(self, agent):
        root = agent.code_workspace.root
        d = root / "many"
        d.mkdir()
        for i in range(60):
            (d / f"f{i:02d}.txt").write_text("x", encoding="utf-8")
        listing = agent.code_workspace.list_dir("")
        assert len(listing.splitlines()) <= 45
        assert "grep" in listing      # 超出时提示用 grep 收窄


class TestBudgetFloorAndSteps:
    def test_budget_floor_skips_doomed_chain(self, agent):
        """owner 池见底：不起必死链，记 budget_floor_wait，零 LLM 消耗。"""
        agent.kernel.pools._remaining["owner_tasks"]["tokens"] = 500.0
        from proto.goals.system import GoalNode
        node = GoalNode(id="g1", level="mid", text="实现 App",
                        acceptance="文件存在")
        agent.llm.decisions = []
        result = agent.work_on_goal(node)
        assert result["status"] == "budget_floor_wait"
        events = [e["event_type"] for e in agent.spine.find(lambda d: True)]
        assert "budget_floor_wait" in events
        assert agent.llm.prompts == []       # 没烧一次 LLM

    def test_work_on_goal_uses_raised_step_budget(self, agent, tmp_path):
        """实现类目标步数上限升到 24/12（源级断言防回退）。"""
        import inspect
        from proto.runtime import agent as agent_mod
        src = inspect.getsource(agent_mod.ProtoAgent._work_on_goal)
        assert "max_steps=24" in src and "max_steps=12" in src

    def test_zero_output_escalation_prefix(self, tmp_path):
        from proto.runtime.agent import _escalation_prefix
        assert _escalation_prefix(0) == ""
        assert _escalation_prefix(1) == ""
        txt = _escalation_prefix(3)
        assert "零产出" in txt and "write_file" in txt and "第 1 步" in txt
