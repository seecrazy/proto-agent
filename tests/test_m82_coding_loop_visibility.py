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
