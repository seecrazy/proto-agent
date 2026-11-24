# -*- coding: utf-8 -*-
"""M85：编码循环的「读经济学」——重读不重执行，重放要挑对工具。

生产实证（2026-11-24 22:21 前后）：交付门以「连续两个工作片无实质变化」暂停
XCTest 目标；两片日志显示 read_skill×7-8、read_files×3、零落盘——专家在反复
翻技能文档找一个不存在的口子（UI 测试 target，同批已补）。重放此前只覆盖命令
类工具，重读文件/技能每次都真实执行并整段塞回上下文。
"""
import shutil

import pytest

from proto.exec.agentic import REPLAY_SAFE_READS, AgenticLoop
from proto.runtime.agent import ProtoAgent
from tests.test_engineering_skills import ScriptedEngineer, tool_step
from tests.test_m82_coding_loop_visibility import read_step, run_slice


def skill_step():
    return tool_step("read_skill", name="ios-app-project")


@pytest.mark.skipif(not shutil.which("sandbox-exec"), reason="真实读取需要 macOS 沙盒")
def test_read_skill_reread_between_reads_is_replayed(tmp_path, monkeypatch):
    agent, _, session = run_slice(tmp_path, monkeypatch,
                                  [skill_step(), read_step(1), skill_step(),
                                   {"done": True, "summary": "技能内容已掌握"}])
    rows = [row for row in session["trace"] if row["tool"] == "read_skill"]
    assert len(rows) == 2 and rows[1]["replay"] is True
    notes = [text for text in session["observations"] if text.startswith("read_skill(")]
    assert "与第 1 步结果相同" in notes[1]
    assert "内容不会因重读而变化" in notes[1] and "用起来去产出" in notes[1]


@pytest.mark.skipif(not shutil.which("sandbox-exec"), reason="真实读取需要 macOS 沙盒")
def test_read_reruns_after_a_write(tmp_path, monkeypatch):
    agent, _, session = run_slice(tmp_path, monkeypatch,
                                  [skill_step(), tool_step("write_file", path="fix.txt", content="v1"),
                                   skill_step(), {"done": True, "summary": "写过了"}])
    rows = [row for row in session["trace"] if row["tool"] == "read_skill"]
    assert len(rows) == 2 and not any(row.get("replay") for row in rows)


def test_replay_safe_reads_are_file_backed_only(tmp_path):
    agent = ProtoAgent(tmp_path, llm=ScriptedEngineer(), goal_interval_s=0)
    loop = AgenticLoop(agent)
    # 文件背书的读取：同参且中间只有读取 → 重放
    row = {"step": 0, "tool": "read_file", "args": {"path": "a.swift"}, "ok": True}
    loop._session = {"trace": [row]}
    assert loop._repeated_verification("read_file", {"path": "a.swift"}) is row
    # 外部世界的读取（黑板/舰队/公司）：重放会说谎，一律不重放
    for tool in ("fleet_status", "blackboard_read", "company_context", "inspect_build"):
        assert loop._repeated_verification(tool, {}) is None
    # 源级锁死集合内容：外部态工具永不进入
    assert set(REPLAY_SAFE_READS) == {"read_file", "read_files", "read_skill", "list_dir",
                                      "grep", "search_symbols", "explore_code"}
    assert not (set(REPLAY_SAFE_READS) & {"blackboard_read", "blackboard_post", "fleet_status",
                                          "company_context", "company_inbox", "company_send"})
