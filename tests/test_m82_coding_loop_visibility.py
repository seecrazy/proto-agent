# -*- coding: utf-8 -*-
"""M82：编码循环可见性与 iOS 交付可达性。

生产实证（2026-11-24）：专家 23 步零写入，根因是压缩后看不到编译错误与文件正文、
重复验证真实重跑、工位清单不含源码；iOS 交付门要求的 .xcodeproj 无从生成。
"""
import json
import shutil
from pathlib import Path

import pytest

from proto.exec import observations as obs
from proto.exec.agentic import LOOP_SYSTEM, AgenticLoop
from proto.runtime.agent import ProtoAgent
from tests.test_engineering_skills import ScriptedEngineer, run_step, tool_step

BASE = "/private/tmp/proto-ws/projects/wengu"


def swift_build_failure():
    return {"exit": 1, "timeout": False, "reason": "nonzero_exit", "backend": "sandbox-exec",
            "command": ["swift", "build"],
            "stdout": "Building for debugging...\n[2 / 10] Wengu\n",
            "stderr": (
                "warning: /Users/u/Library/org.swift.swiftpm/configuration is not accessible or not "
                "writable, disabling user-level caches.\n[5 / 13] Wengu\n"
                "error: SwiftCompile normal arm64 failed with a nonzero exit code.\n"
                f"{BASE}/Sources/Wengu/Models.swift:7:6: \x1b[1;31merror: \x1b[0m\x1b[1;39m'Unique' is only "
                "available in iOS 18 or newer\x1b[0m\n"
                "  5 | @Model\n"
                "  6 | public final class Deck {\n"
                "  7 |     #Unique<Deck>([\\.name])\n"
                "    |      `- error: 'Unique' is only available in iOS 18 or newer\n"
                "  8 |     public var name: String\n"
                "\n"
                f"{BASE}/Sources/Wengu/Models.swift:30:16: error: A default value requires a fully "
                "qualified domain named value (from macro 'Model')\n"
                "  30 |     public var dueDate: Date = .now\n"
                "     |                `- error: A default value requires a fully qualified domain named value\n"
                "error: Build failed\n")}


# ---- Task 1：命令观察整形 -------------------------------------------------

def test_command_view_puts_diagnostics_first():
    view = obs.command_view(swift_build_failure(), 4000, base=BASE)
    assert view.startswith("退出码 1（nonzero_exit）")
    assert "错误摘要 2 条" in view
    assert "Sources/Wengu/Models.swift:7:6: error: 'Unique' is only available in iOS 18 or newer" in view
    assert "#Unique<Deck>([\\.name])" in view            # 源码摘录随诊断一起给
    assert "Sources/Wengu/Models.swift:30:16: error:" in view
    assert BASE not in view                               # 绝对路径裁成工位相对路径
    assert "\x1b[" not in view                            # 颜色码被清掉
    assert view.index("错误摘要") < view.index("输出尾部")
    assert "org.swift.swiftpm/configuration" not in view.split("输出尾部")[0]   # 警告只在尾部
    assert "error: Build failed" not in view.split("错误摘要")[1].split("输出尾部")[0]  # 汇总行不占摘要
    assert len(view.encode("utf-8")) <= 4000


def test_command_view_clips_from_the_tail_not_the_head():
    view = obs.command_view(swift_build_failure(), 900, base=BASE)
    assert view.startswith("退出码 1（nonzero_exit）")
    assert "Models.swift:7:6: error:" in view
    assert len(view.encode("utf-8")) <= 900


def test_command_view_keeps_head_and_tail_for_success():
    result = {"exit": 0, "timeout": False, "reason": "", "backend": "sandbox-exec",
              "command": ["xcodebuild", "-list"],
              "stdout": ("Command line invocation:\n    /Applications/Xcode.app/Contents/Developer/usr/bin/"
                         "xcodebuild -list -project Wengu.xcodeproj\n" + "x" * 3000 +
                         "\nInformation about project \"Wengu\":\n    Targets:\n        Wengu\n\n"
                         "    Schemes:\n        Wengu\n"),
              "stderr": "2026-11-24 xcodebuild[1] [MT] noise\n"}
    view = obs.command_view(result, 4000)
    assert view.startswith("退出码 0")
    assert "Command line invocation:" in view      # 头部同样重要
    assert "Schemes:" in view and "Wengu" in view  # 尾部也保留
    assert "错误摘要" not in view


def test_command_view_reports_timeout_and_missing_exit():
    view = obs.command_view({"exit": -1, "stdout": "", "stderr": "timeout", "timeout": True,
                             "reason": "timeout", "backend": "sandbox-exec"}, 4000)
    assert view.startswith("退出码 -1（timeout）")
    assert obs.command_view("plain text", 4000) == "plain text"


def test_diagnostics_falls_back_to_generic_lines_when_no_location():
    xcode = {"exit": 65, "reason": "nonzero_exit", "stdout": (
        "** BUILD FAILED **\n\nThe following build commands failed:\n"
        "\tSwiftEmitModule normal arm64 Emitting\\ module\\ for\\ Spike (in target 'Spike' from project 'Spike')\n"
        "\tBuilding project Spike with scheme Spike\n(5 failures)\n"),
        "stderr": "2026-11-24 xcodebuild[1] [MT] IDESchemeActionsInvocationRecord: Device is missing x.\n"}
    view = obs.command_view(xcode, 4000)
    assert "The following build commands failed:" in view
    assert "SwiftEmitModule normal arm64" in view
    assert "** BUILD FAILED **" in view
    assert "IDESchemeActionsInvocationRecord" not in view.split("输出尾部")[0]


def test_diagnostics_covers_pytest_python_and_npm():
    pytest_out = ("E   AssertionError: assert 2 == 3\nE    +  where 2 = add(1, 1)\n"
                  "=========================== short test summary info ============================\n"
                  "FAILED tests/test_sample.py::test_add - AssertionError: assert 2 == 3\n"
                  "=================== 1 failed in 0.01s ===================\n")
    assert obs.diagnostics(pytest_out) == [
        "FAILED tests/test_sample.py::test_add - AssertionError: assert 2 == 3"]
    python_out = ("Traceback (most recent call last):\n  File \"app.py\", line 30, in <module>\n"
                  "    raise ValueError('boom')\nValueError: boom\n")
    assert obs.diagnostics(python_out) == [
        '  File "app.py", line 30, in <module>', "ValueError: boom"]
    npm_out = ("npm ERR! code ELIFECYCLE\nnpm ERR! errno 1\nnpm ERR! Failed at the sample@1.0.0 test script.\n")
    assert obs.diagnostics(npm_out) == [
        "npm ERR! code ELIFECYCLE", "npm ERR! Failed at the sample@1.0.0 test script."]


def test_diagnostics_dedupe_cap_and_budget():
    lines = [f"src/f{i}.swift:{i}:1: error: thing {i} wrong" for i in range(30)]
    found = obs.diagnostics("\n".join(lines))
    assert len(found) == 12                      # 条数上限
    assert found[0].startswith("src/f0.swift:0:1: error:")
    assert obs.diagnostics("error: same\nerror: same\n") == ["error: same"]
    long_lines = [f"src/g{i}.swift:1:1: error: {'x' * 600}" for i in range(10)]
    joined = "\n".join(obs.diagnostics("\n".join(long_lines), budget=1500))
    assert "src/g1.swift" in joined and "src/g2.swift" not in joined
    assert len(joined.encode("utf-8")) <= 1500


def test_failure_key_tracks_diagnostics_not_banner():
    first = swift_build_failure()
    second = swift_build_failure()
    second["stderr"] = second["stderr"].replace("'Unique' is only available", "'Unique' is unavailable in")
    assert obs.failure_key(first, base=BASE) != obs.failure_key(second, base=BASE)
    same_again = swift_build_failure()
    same_again["stdout"] = "Building for debugging...\n[1 / 9]\n"      # 进度行变化不算
    assert obs.failure_key(first, base=BASE) == obs.failure_key(same_again, base=BASE)
    assert "Models.swift:7:6" in obs.failure_key(first, base=BASE)


@pytest.mark.skipif(not shutil.which("sandbox-exec"), reason="真实命令执行需要 macOS 沙盒")
def test_agent_observation_uses_shaped_command_view(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTO_EXEC_BACKEND", "sandbox-exec")
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    model = ScriptedEngineer([
        {"tool": "run_command", "args": {"argv": ["python3", "-c", "raise SystemExit('boom')"], "cwd": ""}},
        {"done": True, "summary": "看了"}])
    agent = ProtoAgent(tmp_path, llm=model, goal_interval_s=0)
    result = AgenticLoop(agent, max_steps=4).run("本地运行一段会失败的脚本", "看到失败原因")
    session = agent.sessions.lookup(result["session_id"])
    note = [text for text in session["observations"] if text.startswith("run_command(")][-1]
    assert "退出码 1（nonzero_exit）" in note
    assert "boom" in note
    assert "{'exit'" not in note


# ---- Task 2：压缩策略按类别保留 -------------------------------------------

from proto.exec import work_context as wc  # noqa: E402

BUILD_ARGS = '{"cwd": "projects/wengu", "程序": "swift"}'


def read_obs(path, marker, lines=60):
    body = "\n".join(f"{n}\t{marker} line {n} public var value{n}: Int = {n}" for n in range(1, lines + 1))
    return f'read_file({{"path": "{path}", "offset": 0, "limit": {lines}}}) → {body}\n〔共 {lines} 行，已读第 1-{lines} 行〕'


def build_obs(*errors):
    digest = "\n".join(errors)
    return (f'run_command({BUILD_ARGS}) → 退出码 1（nonzero_exit）\n─ 错误摘要 {len(errors)} 条 ─\n'
            f"{digest}\n─ 输出尾部 ─\nerror: Build failed\n")


def compacted(observations, tight=False):
    return "\n".join(wc._compact_observations(observations, [], tight))


def test_compact_keeps_recent_file_bodies_and_errors():
    observations = [
        read_obs("projects/wengu/Sources/Wengu/Models.swift", "MODELS", 30),
        read_obs("projects/wengu/Sources/Wengu/WenguApp.swift", "APP"),
        build_obs("Sources/Wengu/Models.swift:7:6: error: 'Unique' is only available in iOS 18 or newer",
                  "Sources/Wengu/WenguApp.swift:20:13: error: cannot find 'DeckListView' in scope"),
    ]
    original = list(observations)
    text = compacted(observations)
    assert "MODELS line 1" in text and "APP line 1" in text       # 两个最近读过的文件正文可见
    assert "Models.swift:7:6: error:" in text and "DeckListView" in text
    assert text.index("MODELS line 1") < text.index("APP line 1") < text.index("错误摘要")   # 仍按时间顺序
    assert text.startswith("历史摘要")
    assert observations == original                                 # 原观察不被改写


def test_compact_marks_stale_reads_after_write():
    observations = [read_obs("projects/wengu/a.swift", "ALINE", 20),
                    'edit_file({"path": "projects/wengu/a.swift"}) → 1',
                    build_obs("a.swift:3:1: error: boom")]
    text = compacted(observations)
    assert "ALINE" not in text                                      # 过期正文不再给
    assert "a.swift" in text and "已过期" in text
    assert "a.swift:3:1: error:" in text


def test_compact_keeps_only_latest_read_of_same_file():
    observations = [read_obs("projects/wengu/a.swift", "OLD"),
                    'read_files({"paths": ["projects/wengu/b.swift"]}) → ### b\nB BODY',
                    read_obs("projects/wengu/a.swift", "NEW")]
    text = compacted(observations)
    assert "NEW line 1" in text and "OLD line" not in text
    assert "B BODY" in text


def test_compact_folds_older_files_with_a_way_back():
    observations = [read_obs("projects/wengu/one.swift", "ONE"),
                    read_obs("projects/wengu/two.swift", "TWO"),
                    read_obs("projects/wengu/three.swift", "THREE")]
    text = compacted(observations)
    assert "ONE line" not in text and "TWO line 1" in text and "THREE line 1" in text
    folded = [line for line in text.splitlines() if "one.swift" in line]
    assert folded and "正文已折叠" in folded[0] and "read_file(" in folded[0] and "offset" in folded[0]


def test_compact_truncates_long_recent_file_and_says_so():
    observations = [read_obs("projects/wengu/big.swift", "BIG", 400)]
    text = compacted(observations)
    assert "BIG line 1" in text and "BIG line 399" not in text
    assert "截断" in text and "offset" in text


def test_compact_stays_within_budget_with_many_steps():
    observations = []
    for index in range(12):
        observations += [read_obs(f"projects/wengu/f{index}.swift", f"F{index}"),
                         build_obs(f"f{index}.swift:1:1: error: number {index}")]
    text = compacted(observations)
    assert wc.size(text) <= wc.COMPACT_HISTORY_BYTES
    assert "f11.swift:1:1: error:" in text                    # 最新一条验证结果一定在
    tight = compacted(observations, tight=True)
    assert wc.size(tight) <= wc.TIGHT_HISTORY_BYTES and " line 1 " not in tight


def test_budgets_match_spec(monkeypatch):
    monkeypatch.delenv("PROTO_ACTOR_INPUT_TOKENS", raising=False)
    assert wc.MAX_HISTORY_BYTES == 12 * 1024
    assert wc.COMPACT_HISTORY_BYTES == 12 * 1024
    assert wc.TIGHT_HISTORY_BYTES == 3 * 1024
    assert wc.actor_input_budget() == 65536
    monkeypatch.setenv("PROTO_ACTOR_INPUT_TOKENS", "999999999")
    assert wc.actor_input_budget() == wc.MAX_ACTOR_BYTES


def test_decision_input_compacts_once_history_exceeds_the_raw_ceiling():
    observations = [read_obs("projects/wengu/old.swift", "OLD", 400),
                    read_obs("projects/wengu/mid.swift", "MID"),
                    read_obs("projects/wengu/Sources/Wengu/Models.swift", "MODELS", 30),
                    build_obs("Sources/Wengu/Models.swift:7:6: error: 'Unique' is only available in iOS 18 or newer")]
    assert sum(wc.size(text) + 8 for text in observations) > wc.MAX_HISTORY_BYTES
    output = wc.decision_input("系统", "固定规则：不许静默裁剪。", "辅助信息" * 300, observations, [], 65536)
    assert "OLD line" not in output and "MODELS line 1" in output
    assert "Models.swift:7:6: error:" in output and "固定规则" in output and "辅助信息" in output
    assert wc.size("系统" + output) <= 65536


# ---- Task 3：重复验证重放与判重 ---------------------------------------------

COUNT = "open('count.txt','a').write('x'); "
BANNER = "print('B' * 150); "


def failing(reason):
    return run_step("python3", "-c", BANNER + COUNT + f"import sys; sys.exit({reason!r})")


def read_step(index):
    return tool_step("read_file", path=f"notes{index}.txt")


def run_slice(tmp_path, monkeypatch, steps, task="验证一段会失败的脚本"):
    monkeypatch.setenv("PROTO_EXEC_BACKEND", "sandbox-exec")
    model = ScriptedEngineer(list(steps))
    agent = ProtoAgent(tmp_path, llm=model, goal_interval_s=0)
    result = AgenticLoop(agent, max_steps=12).run(task, "看到失败原因")
    return agent, result, agent.sessions.lookup(result["session_id"])


def executions(agent):
    """命令在 cwd="" 下写工作区根；每个 x 是一次真实执行。"""
    counter = agent.code_workspace.root / "count.txt"
    return counter.read_text() if counter.exists() else ""


@pytest.mark.skipif(not shutil.which("sandbox-exec"), reason="真实命令执行需要 macOS 沙盒")
def test_repeated_verification_replays_after_read_only_steps(tmp_path, monkeypatch):
    agent, result, session = run_slice(tmp_path, monkeypatch,
                                       [failing("first"), read_step(1), failing("first"), {"done": True, "summary": "还坏着"}])
    assert executions(agent) == "x"                            # 只真实执行了一次
    rows = [row for row in session["trace"] if row["tool"] == "run_command"]
    assert len(rows) == 2 and rows[1]["replay"] is True and rows[1]["step"] == 2
    notes = [text for text in session["observations"] if text.startswith("run_command(")]
    assert "与第 1 步结果相同" in notes[1] and "只做过读取" in notes[1]
    assert "first" in notes[1]                                  # 重放时带回原来的失败原因
    assert result["trace"][2]["replay"] is True                 # 时间线也能看出这是重放


@pytest.mark.skipif(not shutil.which("sandbox-exec"), reason="真实命令执行需要 macOS 沙盒")
def test_repeated_verification_reruns_after_write_or_other_command(tmp_path, monkeypatch):
    agent, _, session = run_slice(tmp_path, monkeypatch, [
        failing("first"), tool_step("write_file", path="fix.txt", content="v2"), failing("first"),
        read_step(1), run_step("python3", "-c", "print('other')"), read_step(2), failing("first"),
        {"done": True, "summary": "改过了"}])
    assert executions(agent) == "xxx"                          # 写入之后、别的命令之后都真实重跑
    assert not any(row.get("replay") for row in session["trace"])


def test_repeated_verification_skips_timeouts_and_unrecorded_outcomes(tmp_path):
    agent = ProtoAgent(tmp_path, llm=ScriptedEngineer(), goal_interval_s=0)
    loop = AgenticLoop(agent)
    args = {"argv": ["swift", "build"], "cwd": "p"}
    row = {"step": 0, "tool": "run_command", "args": args, "ok": True}
    loop._session = {"trace": [dict(row, outcome={"exit": -1, "timeout": True, "reason": "timeout"})]}
    assert loop._repeated_verification("run_command", args) is None     # 超时的结果不能重放
    loop._session = {"trace": [dict(row)]}
    assert loop._repeated_verification("run_command", args) is None     # 旧会话没有 outcome：保守不重放
    finished = dict(row, outcome={"exit": 1, "timeout": False, "reason": "nonzero_exit"})
    loop._session = {"trace": [finished, {"step": 1, "tool": "read_file", "args": {"path": "a"}, "ok": True}]}
    assert loop._repeated_verification("run_command", args) is finished
    assert loop._repeated_verification("write_file", {"path": "a"}) is None
    loop._session = {"trace": [finished]}
    assert loop._repeated_verification("run_command", args, ios_required=True) is None   # 交付取证命令要求全新的构建目录


@pytest.mark.skipif(not shutil.which("sandbox-exec"), reason="真实命令执行需要 macOS 沙盒")
def test_fail_repeat_hint_counts_identical_failures_only(tmp_path, monkeypatch):
    agent, _, session = run_slice(tmp_path, monkeypatch, [
        failing("reason A"), read_step(1),
        failing("reason B"), read_step(2), failing("reason B"), read_step(3), failing("reason B"),
        {"done": True, "summary": "同一处反复失败"}])
    hints = [text for text in session["observations"] if "3 次以相同方式失败" in text]
    assert len(hints) == 1 and "reason B" in hints[0]           # 按诊断计数：B 的第三次才提醒，A 不混入


@pytest.mark.skipif(not shutil.which("sandbox-exec"), reason="真实命令执行需要 macOS 沙盒")
def test_eight_dry_steps_point_at_the_first_error(tmp_path, monkeypatch):
    compiler = run_step("python3", "-c", "import sys; sys.stderr.write('src/a.swift:7:6: error: broken thing\\n'); sys.exit(1)")
    steps = [compiler] + [read_step(index) for index in range(1, 8)] + [{"done": True, "summary": "只是读"}]
    agent, _, session = run_slice(tmp_path, monkeypatch, steps)
    pointer = [text for text in session["observations"] if "连续 8 步没有落盘" in text]
    assert len(pointer) == 1 and "src/a.swift:7:6" in pointer[0] and "edit_file" in pointer[0]
