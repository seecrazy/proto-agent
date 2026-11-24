# -*- coding: utf-8 -*-
"""M82：编码循环可见性与 iOS 交付可达性。

生产实证（2026-11-24）：专家 23 步零写入，根因是压缩后看不到编译错误与文件正文、
重复验证真实重跑、工位清单不含源码；iOS 交付门要求的 .xcodeproj 无从生成。
"""
import json
import shutil
import socket
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


def test_compact_keeps_stale_read_but_flags_it():
    # 真实模型第二轮：编辑成功后把读过的正文折叠掉，模型看不到文件、也不知道刚才的
    # 修改已生效，于是对着同一个问题盲改了 6 次。正文大部分仍然有效，保留并标出改动。
    observations = [read_obs("projects/wengu/a.swift", "ALINE", 20),
                    'edit_file({"path": "projects/wengu/a.swift"}) → 1',
                    build_obs("a.swift:3:1: error: boom")]
    text = compacted(observations)
    assert "ALINE line 1" in text
    flagged = [line for line in text.splitlines() if "a.swift" in line and "此后该文件被修改过" in line]
    assert flagged
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


# ---- Task 4：工位清单与接口轮廓 ---------------------------------------------

from proto.exec import outline  # noqa: E402
from proto.tools.codetools import CodeTools  # noqa: E402

PROJECT = "projects/wengu"


def seed_project(root, extra=""):
    for path in ("Sources/Wengu/Models.swift", "Sources/Wengu/App.swift",
                 "Tests/WenguTests/EngineTests.swift", "Package.swift",
                 ".build/debug.swift", "Sources/Wengu/.hidden/x.swift"):
        root.write_file(f"{PROJECT}/{path}", extra or "x")


def test_list_dir_project_shows_nested_sources(tmp_path):
    tools = CodeTools(tmp_path)
    seed_project(tools)
    listing = tools.list_dir(PROJECT)
    assert "Sources/Wengu/Models.swift" in listing and "Sources/Wengu/App.swift" in listing
    assert "Tests/WenguTests/EngineTests.swift" in listing and "Package.swift" in listing
    assert ".build" not in listing and ".hidden" not in listing
    root = tools.list_dir()
    assert "projects/" in root and "projects/wengu/" in root and "wengu/Package.swift" in root
    assert "Sources/" not in root                 # 根目录清单保持「目录 2 层 + 项目内文件 1 层」


def test_prompt_block_lists_project_sources(tmp_path):
    model = ScriptedEngineer([tool_step("read_file", path=f"{PROJECT}/Package.swift"), {"done": True, "summary": "看了"}])
    agent = ProtoAgent(tmp_path, llm=model, goal_interval_s=0)
    seed_project(agent.code_workspace)
    result = AgenticLoop(agent, max_steps=4).run("把工程修到能编译", "swift build 退出 0", project_dir=PROJECT)
    blocks = agent.sessions.lookup(result["session_id"])["prompt_blocks"]
    listing = [block for block in blocks if "工位文件" in block][0]
    assert "Sources/Wengu/Models.swift" in listing and "Tests/WenguTests/EngineTests.swift" in listing
    assert "开工时" in listing


SWIFT = '''import Foundation

@Model
public struct Deck: Identifiable {
    public var id: UUID
    public var name: String = ""
    public init(name: String) { self.name = name }
    public func rename(_ value: String) { name = value }
}

final class Engine {
    static let shared = Engine()
    func schedule(_ quality: Int, from date: Date) -> Int { quality }
    struct State { var interval: Int }
}

extension Engine {
    public func review() {}
}
'''


def test_swift_outline_lists_types_and_members():
    found = outline.swift_outline(SWIFT)
    assert [header for header, _ in found] == [
        "struct Deck", "class Engine", "struct State", "extension Engine"]
    assert found[0][1] == ["var id: UUID", "var name: String", "init(name: String)",
                           "func rename(_ value: String)"]
    assert found[1][1][:2] == ["static let shared", "func schedule(_ quality: Int, from date: Date) -> Int"]
    assert found[2][1] == ["var interval: Int"]
    assert found[3][1] == ["func review()"]


def test_outline_covers_python_and_javascript():
    python = "import os\n\n\nclass Engine:\n    def schedule(self, quality: int) -> int:\n        return quality\n\n    def helper():\n        pass\n"
    found = outline.python_outline(python)
    assert found[0][0] == "class Engine"
    assert found[0][1] == ["schedule(self, quality: int) -> int", "helper()"]     # 注解与返回类型都在
    assert outline.code_outline(Path("a.js"), "export function run(a, b) { return a }\nconst limit = 3\nclass Foo {}\n") == [
        ("function run", ["run(a, b)"]), ("const limit", ["limit"]), ("class Foo", [])]


def outline_block(prompt):
    """固定区里 [工位接口轮廓]…[/工位接口轮廓] 之间的文本。"""
    return prompt.split("[工位接口轮廓]", 1)[1].split("[/工位接口轮廓]", 1)[0]


def test_outline_block_follows_source_changes_and_stays_bounded(tmp_path):
    first = "struct Card {\n    var front: String\n}\n"
    second = "struct Card {\n    var front: String\n    func schedule() {}\n}\n"
    model = ScriptedEngineer([
        read_step(1),
        tool_step("write_file", path=f"{PROJECT}/Sources/Wengu/Models.swift", content=second),
        read_step(2),
        {"done": True, "summary": "改好了"}])
    agent = ProtoAgent(tmp_path, llm=model, goal_interval_s=0)
    seed_project(agent.code_workspace)
    agent.code_workspace.write_file(f"{PROJECT}/Sources/Wengu/Models.swift", first)
    result = AgenticLoop(agent, max_steps=8).run("把工程修到能编译", "swift build 退出 0", project_dir=PROJECT)
    inputs = model.actor_inputs
    assert "struct Card" in outline_block(inputs[0])
    assert "func schedule()" not in outline_block(inputs[0]) and "func schedule()" in outline_block(inputs[-1])
    block = outline_block(inputs[-1])                         # 源码变了轮廓跟着变
    assert "var front: String" in block and "Models.swift" in block
    assert len(block.encode("utf-8")) <= 2400 + 80


def test_outline_block_is_bounded_and_absent_without_sources(tmp_path):
    many = "".join(f"struct Thing{index} {{\n    var value{index}: Int\n    func run{index}() {{}}\n}}\n\n"
                   for index in range(200))
    model = ScriptedEngineer([{"done": True, "summary": "只是看看"}])
    agent = ProtoAgent(tmp_path, llm=model, goal_interval_s=0)
    agent.code_workspace.write_file(f"{PROJECT}/Sources/Wengu/Big.swift", many)
    agent.code_workspace.write_file(f"{PROJECT}/README.md", "# Wengu")
    result = AgenticLoop(agent, max_steps=2).run("看看工程", "", project_dir=PROJECT)
    block = outline_block(model.actor_inputs[0])
    assert len(block.encode("utf-8")) <= 2400 + 80 and "轮廓已截断" in block and "Thing199" not in block
    docs = ScriptedEngineer([{"done": True, "summary": "只是文档"}])
    other = ProtoAgent(tmp_path / "docs", llm=docs, goal_interval_s=0)
    other.code_workspace.write_file("projects/manual/README.md", "# 手册")
    AgenticLoop(other, max_steps=2).run("看看文档", "", project_dir="projects/manual")
    assert "[工位接口轮廓]" not in docs.actor_inputs[0]


# ---- Task 5：沙盒放行与 xcodebuild 旗标 -------------------------------------

import subprocess  # noqa: E402
import tempfile  # noqa: E402
import uuid  # noqa: E402

MACOS_SANDBOX = pytest.mark.skipif(
    not shutil.which("sandbox-exec") or not shutil.which("xcodebuild") or not shutil.which("python3"),
    reason="需要 macOS 沙盒、Xcode 与 python3")
PROFILE_RULES = (
    '(allow file-write* (regex #"^/private/var/tmp/com\\.apple\\.launchd\\.[^/]+/"))',
    "(allow network-outbound (remote unix-socket))",
    '(allow file-write* file-ioctl (literal "/dev/ptmx"))',
    '(allow file-read* file-write* file-ioctl (regex #"^/dev/ttys[0-9]+$"))',
)
XCODEBUILD = ["xcodebuild", "-project", "Wengu.xcodeproj", "-scheme", "Wengu", "-destination",
              "generic/platform=iOS Simulator", "-derivedDataPath", ".proto-build-01"]
FLAG = "OTHER_SWIFT_FLAGS=-Xfrontend -disable-sandbox"


def fake_runner(monkeypatch, tmp_path, backend="sandbox-exec"):
    monkeypatch.setenv("PROTO_EXEC_BACKEND", backend)
    (tmp_path / "p").mkdir(exist_ok=True)
    tools = CodeTools(tmp_path)
    monkeypatch.setattr(CodeTools, "_sandbox_exec_profile", lambda self, tmp: "(version 1)\n(allow default)\n")
    seen = {}

    def run(cmd, **kwargs):
        seen["cmd"], seen["timeout"] = cmd, kwargs.get("timeout")
        return subprocess.CompletedProcess(cmd, 0, stdout=b"** BUILD SUCCEEDED **", stderr=b"")

    monkeypatch.setattr("proto.tools.codetools.subprocess.run", run)
    return tools, seen


@MACOS_SANDBOX
def test_xcodebuild_gets_macro_sandbox_flag_under_sandbox_exec(tmp_path, monkeypatch):
    tools, seen = fake_runner(monkeypatch, tmp_path)
    result = tools.run_command(XCODEBUILD + ["build"], cwd="p")
    assert seen["cmd"][-1] == FLAG and result["command"][-1] == FLAG
    mixed = tools.run_command(XCODEBUILD + ["OTHER_SWIFT_FLAGS=-Onone", "test"], cwd="p")
    assert mixed["command"][-2:] == ["OTHER_SWIFT_FLAGS=-Onone -Xfrontend -disable-sandbox", "test"]
    kept = tools.run_command(XCODEBUILD + ["OTHER_SWIFT_FLAGS=-Xfrontend -disable-sandbox", "build"], cwd="p")
    assert kept["command"].count(FLAG) == 1 and kept["command"][-2] == FLAG
    via_xcrun = tools.run_command(["xcrun", "xcodebuild"] + XCODEBUILD[1:] + ["test"], cwd="p")
    assert via_xcrun["command"][:2] == ["xcrun", "xcodebuild"] and via_xcrun["command"][-1] == FLAG


@MACOS_SANDBOX
def test_xcodebuild_list_and_other_backends_stay_untouched(tmp_path, monkeypatch):
    tools, seen = fake_runner(monkeypatch, tmp_path)
    listed = tools.run_command(XCODEBUILD[:3] + ["-list"], cwd="p")
    assert FLAG not in listed["command"] and seen["cmd"][-1] == "-list"
    shown = tools.run_command(["xcodebuild", "-showsdks"], cwd="p")
    assert FLAG not in shown["command"]
    tools, _ = fake_runner(monkeypatch, tmp_path, backend="subprocess")
    plain = tools.run_command(XCODEBUILD + ["build"], cwd="p")
    assert FLAG not in plain["command"] and plain["backend"] == "subprocess"


@MACOS_SANDBOX
def test_xcodebuild_test_gets_a_cold_start_timeout(tmp_path, monkeypatch):
    tools, seen = fake_runner(monkeypatch, tmp_path)
    tools.run_command(XCODEBUILD + ["test"], cwd="p")
    assert seen["timeout"] == 900                    # 冷启动模拟器首次测试约 8 分钟才就绪
    tools.run_command(XCODEBUILD + ["build"], cwd="p")
    assert seen["timeout"] == 120                    # 普通构建默认不变
    tools.run_command(XCODEBUILD + ["build"], cwd="p", timeout_s=999)
    assert seen["timeout"] == 300                    # 非测试动作上限 300 秒
    tools.run_command(["swift", "build"], cwd="p", timeout_s=999)
    assert seen["timeout"] == 300
    tools.run_command(XCODEBUILD + ["test"], cwd="p", timeout_s=100)
    assert seen["timeout"] == 100                    # 显式给的更短时间照办


def test_sandbox_profile_adds_only_local_ipc_rules():
    tools = CodeTools(Path(tempfile.mkdtemp()))
    profile = tools._sandbox_exec_profile("/tmp/proto-profile-probe")
    assert "(deny file-write*)" in profile and "(deny network*)" in profile
    for rule in PROFILE_RULES:
        assert rule in profile
    assert "(allow network*)" not in profile and "remote ip" not in profile


@MACOS_SANDBOX
def test_sandbox_still_denies_outside_writes_and_tcp(tmp_path):
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setenv("PROTO_EXEC_BACKEND", "sandbox-exec")
        tools = CodeTools(tmp_path)
        # pytest 的临时目录本身就在 Darwin 用户临时目录里（沙盒放行），要拿它外面的路径验拒绝
        outside = Path("/tmp") / f"proto-outside-{uuid.uuid4().hex}"
        outside.mkdir()
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        code = (
            "import json, os, pty, socket\n"
            "out = {}\n"
            "try:\n"
            "    pty.openpty(); out['pty'] = 'ok'\n"
            "except OSError as exc:\n"
            "    out['pty'] = repr(exc)\n"
            "try:\n"
            "    open(os.path.join(%r, 'x.txt'), 'w').write('x'); out['outside'] = 'wrote'\n"
            "except OSError as exc:\n"
            "    out['outside'] = type(exc).__name__\n"
            "try:\n"
            "    socket.create_connection(('127.0.0.1', %d), timeout=3).close(); out['tcp'] = 'connected'\n"
            "except OSError as exc:\n"
            "    out['tcp'] = type(exc).__name__\n"
            "print(json.dumps(out))\n" % (str(outside), listener.getsockname()[1]))
        result = tools.run_command(["python3", "-c", code], cwd="")
        report = json.loads(result["stdout"])
        assert report == {"pty": "ok", "outside": "PermissionError", "tcp": "PermissionError"}, result
    finally:
        listener.close()
        monkey.undo()
        shutil.rmtree(outside, ignore_errors=True)


# ---- 原始输出裁剪不丢错误行；shell 语法直接拒并给出路（真实模型首跑发现） -------

def test_run_command_result_carries_diagnostics_from_the_full_output(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTO_EXEC_BACKEND", "sandbox-exec")
    tools = CodeTools(tmp_path)
    (tmp_path / "p").mkdir()
    script = (
        "import sys\n"
        "for i in range(300): sys.stderr.write('noise %d\\n' % i)\n"
        "sys.stderr.write('p/Sources/a.swift:7:6: error: first problem\\n  7 | let x = foo\\n  8 | next\\n')\n"
        "for i in range(300): sys.stderr.write('more noise %d\\n' % i)\n"
        "sys.exit(1)\n")
    result = tools.run_command(["python3", "-c", script], cwd="p")
    assert "p/Sources/a.swift:7:6: error: first problem" in "\n".join(result["diagnostics"])
    assert "  7 | let x = foo" in "\n".join(result["diagnostics"])          # 源码摘录跟着诊断走
    assert result["diagnostics"]                                   # 即使头尾裁剪把报错裁掉
    assert len(result["stderr"]) < 4000


@MACOS_SANDBOX
def test_run_command_report_keeps_errors_hidden_by_a_long_tail(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTO_EXEC_BACKEND", "sandbox-exec")
    tools = CodeTools(tmp_path)
    script = (
        "import sys\n"
        "print('building...')\n"
        "for i in range(400): sys.stderr.write('note line %d about nothing at all\\n' % i)\n"
        "sys.stderr.write('src/a.swift:7:6: error: cannot find foo in scope\\n  7 | let x = foo\\n')\n"
        "for i in range(400): sys.stderr.write('more note %d about nothing at all\\n' % i)\n"
        "sys.stderr.write('error: Build failed\\n')\n"
        "sys.exit(1)\n")
    view = obs.command_view(tools.run_command(["python3", "-c", script], cwd=""))
    assert "错误摘要 1 条" in view and "src/a.swift:7:6: error: cannot find foo in scope" in view


@pytest.mark.skipif(not shutil.which("swift") or not shutil.which("xcrun"), reason="需要 Xcode 工具链")
def test_run_command_rejects_shell_syntax_with_a_way_forward(tmp_path):
    tools = CodeTools(tmp_path)
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "Notes.swift").write_text("let a = 1\n")
    redirect = tools.run_command(["swift", "build", "2>&1"], cwd="p")
    assert redirect["exit"] == -1 and redirect["reason"] == "shell_syntax_not_supported"
    assert "不经过 shell" in redirect["stderr"] and "stdout" in redirect["stderr"]
    glob = tools.run_command(["xcrun", "swiftc", "-typecheck", "Sources/Wengu/*.swift"], cwd="p")
    assert glob["reason"] == "shell_syntax_not_supported" and "通配符" in glob["stderr"]
    for argv in (["xcodebuild", "-list", "|", "head"], ["python3", "script.py", ">", "out.txt"],
                 ["swift", "build", "&&", "swift", "test"]):
        assert tools.run_command(argv, cwd="p")["reason"] == "shell_syntax_not_supported", argv
    # 代码里的比较符、正则里的 *、真实存在的路径都不算 shell 语法
    for argv in (["python3", "-c", "print(1 > 0)"], ["swift", "test", "--filter", "WenguTests.Suite"],
                 ["xcrun", "swiftc", "-typecheck", "p/Notes.swift"]):
        assert tools.run_command(argv, cwd="")["reason"] != "shell_syntax_not_supported", argv


# ---- 真实模型第二轮：编辑反馈与容错 ------------------------------------------

def test_edit_file_tolerates_indentation_and_reports_already_applied(tmp_path):
    tools = CodeTools(tmp_path)
    tools.write_file("a.swift", "struct Deck {\n\tlet name: String\n}\n")
    assert tools.edit_file("a.swift", "    let name: String", "    let id: UUID") == 1   # tab 与空格不同也能唯一命中
    assert (tmp_path / "a.swift").read_text() == "struct Deck {\n    let id: UUID\n}\n"
    with pytest.raises(ValueError, match="已经生效"):
        tools.edit_file("a.swift", "    let name: String", "    let id: UUID")
    with pytest.raises(ValueError) as info:
        tools.edit_file("a.swift", "    let nmae: UUID", "x")
    assert "最接近" in str(info.value) and "let id: UUID" in str(info.value)
    tools.write_file("b.swift", "let a = 1\nlet a = 1\n")
    with pytest.raises(ValueError, match="出现 2 次"):
        tools.edit_file("b.swift", "  let a = 1", "let b = 2")


def session_of(agent, result):
    return agent.sessions.lookup(result["session_id"])


def test_edit_observation_shows_what_changed(tmp_path):
    model = ScriptedEngineer([tool_step("edit_file", path="a.txt", old="old line", new="new line"),
                              tool_step("write_file", path="b.txt", content="12345"),
                              {"done": True, "summary": "改了"}])
    agent = ProtoAgent(tmp_path, llm=model, goal_interval_s=0)
    agent.code_workspace.write_file("a.txt", "old line\nother\n")
    result = AgenticLoop(agent, max_steps=6).run("把 old line 换成 new line", "")
    notes = [text for text in session_of(agent, result)["observations"] if text.split("(")[0] in ("edit_file", "write_file")]
    assert "已替换 1 处" in notes[0] and "old line" in notes[0] and "new line" in notes[0]
    assert "已写入 b.txt" in notes[1] and "5 字符" in notes[1]
    rows = [row for row in result["trace"] if row["tool"] in ("edit_file", "write_file")]
    assert [row["written_path"] for row in rows] == ["a.txt", "b.txt"]
    assert [row["result"] for row in rows] == ["1", "written:b.txt"]      # 轨迹行仍是工具原始输出
