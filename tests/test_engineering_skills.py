import hashlib
import json
import os
import shutil
import sys
from importlib.resources import files
from pathlib import Path

import pytest

from proto.exec.agentic import LOOP_SYSTEM, AgenticLoop
from proto.tools.codetools import CodeTools
from proto.exec.engineering import (capability_question, command_check, inspect_build, requested_checks,
                                    verification_receipt)
from proto.kernel.kernel import ActionCategory
from proto.runtime.agent import ProtoAgent
from proto.fleet.manager import AgentManager
from proto.skills import read_skill
from proto.system.execution_log import execution_logging
from proto.work.delivery import ios_build_start, main_project, valid_ios_evidence
from tests.test_m66_fleet_runs import console
from tests.test_web import call


class ScriptedEngineer:
    def __init__(self, decisions=()):
        self.decisions = list(decisions)
        self.messages = []

    def complete(self, system, user):
        self.messages.append((system, user))
        if system == LOOP_SYSTEM:
            step = self.decisions.pop(0) if self.decisions else {"done": True, "summary": "无法在本地编译"}
            if isinstance(step, BaseException):
                raise step
            return json.dumps(step, ensure_ascii=False)
        if "独立验收" in system:
            return '{"verdict":"pass"}'
        return "先确认工位和检查命令，再做最小验证"

    @property
    def actor_inputs(self):
        return [user for system, user in self.messages if system == LOOP_SYSTEM]


def tool_step(tool, **args):
    return {"tool": tool, "args": args}


def run_step(*argv, cwd=""):
    return tool_step("run_command", cwd=cwd, argv=list(argv))


def make_manifest(folder, location="group:FixtureApp.xcodeproj"):
    workspace = folder / "Fixture.xcworkspace"
    workspace.mkdir(exist_ok=True)
    manifest = workspace / "contents.xcworkspacedata"
    manifest.write_text(f'<Workspace version="1.0"><FileRef location="{location}"/></Workspace>', encoding="utf-8")
    return manifest


def test_bundled_skill_catalog_and_content_cannot_be_shadowed_by_workspace(tmp_path):
    agent = ProtoAgent(tmp_path)
    name = "swift-ios-build"
    agent.code_workspace.write_file(f"{name}/SKILL.md", "UNTRUSTED WORKSPACE PLAYBOOK")
    definition = agent.tools.definition("read_skill")
    assert definition["category"] == "read_draft"
    assert all(skill in definition["description"] for skill in (name, "local-verification", "design-development-verification"))
    assert "CODE_SIGNING_ALLOWED" not in definition["description"]
    skill = agent.tools.get("read_skill")(name)
    data = files("proto.skills").joinpath(name).joinpath("SKILL.md").read_bytes()
    assert skill["sha256"] == hashlib.sha256(data).hexdigest()
    assert "XCTest" in skill["instructions"] and "UNTRUSTED" not in skill["instructions"]
    with pytest.raises(ValueError, match="unknown_skill"):
        read_skill(f"../{name}")


def test_probe_finds_project_entries_and_same_runner_without_executing_scripts(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTO_EXEC_BACKEND", "sandbox-exec")
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}" if name in
                        {"sandbox-exec", "xcodebuild", "swift", "python3", "pnpm"} else None)

    def no_process(*args, **kwargs):
        pytest.fail("inspect_build must not start any process")

    monkeypatch.setattr("proto.tools.codetools.subprocess.run", no_process)
    tools = CodeTools(tmp_path / "work")
    for path in ("app/App.xcodeproj/project.pbxproj", "app/Package.swift", "app/pyproject.toml", "app/Cargo.toml"):
        tools.write_file(path, "fixture")
    tools.write_file("app/package.json", '{"packageManager":"pnpm@9","scripts":{"build":"secret source","test":" "}}')
    report = inspect_build(tools, "app")
    assert report["runner"] == "isolated" and report["project_state"] == "discovered"
    assert {item["kind"] for item in report["inputs"]} == {"xcode", "swift-package", "python", "rust", "javascript"}
    assert all(check["cwd"] == "app" for check in report["next"])
    assert {tuple(row["argv"][:2]) for row in report["next"]} >= {("xcodebuild", "-showsdks"),
            ("xcodebuild", "-project"), ("swift", "build"), ("swift", "test"), ("python3", "-m")}
    assert "secret source" not in str(report) and report["programs"]["xcodebuild"]
    make_manifest(tools.root / "app", "group:App.xcodeproj")
    assert not any("-list" in row["argv"] for row in inspect_build(tools, "app")["next"])
    assert len([item for item in inspect_build(tools, "app")["inputs"] if item["kind"] == "xcode"]) == 2


@pytest.mark.parametrize(("backend", "which", "state"), [
    ("none", {"xcodebuild"}, "unavailable"), ("sandbox-exec", {"xcodebuild"}, "unavailable"),
    ("subprocess", {"xcodebuild"}, "owner_approval"), ("sandbox-exec", {"sandbox-exec"}, "isolated"),
])
def test_probe_separates_runner_command_path_and_approval(tmp_path, monkeypatch, backend, which, state):
    monkeypatch.setenv("PROTO_EXEC_BACKEND", backend)
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}" if name in which else None)
    agent = ProtoAgent(tmp_path)
    agent.code_workspace.write_file("App.xcodeproj/project.pbxproj", "fixture")
    report = inspect_build(agent.code_workspace)
    assert report["runner"] == state and report["programs"]["xcodebuild"] == ("xcodebuild" in which)
    if state == "owner_approval":
        policy = agent.tools.policy("run_command")
        assert report["next"] and policy.owner_only and policy.category is ActionCategory.VISIBLE_WRITE
    else:
        assert report["next"] == []
        reason = "executable_not_found" if state == "isolated" else "execution_disabled"
        assert agent.code_workspace.run_command(["xcodebuild", "-version"])["reason"] == reason


def test_probe_and_command_do_not_expose_paths_outside_the_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTO_EXEC_BACKEND", "none")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "project.pbxproj").write_text("unrelated private file")
    tools = CodeTools(tmp_path / "workspace")
    (tools.root / "Foreign.xcodeproj").symlink_to(outside)
    assert inspect_build(tools)["inputs"] == []
    assert inspect_build(tools, "Foreign.xcodeproj")["project_state"] == "directory_unavailable"
    assert tools.run_command(["python3", "--version"], cwd="../outside")["reason"] == "cwd_outside_workspace"
    assert tools.run_command(["sh", "-c", "true"])["reason"] == "command_not_allowed"
    assert tools.run_command(["xcrun", "sh"])["reason"] == "xcrun_tool_not_allowed"


def test_probe_marks_large_directory_scan_as_partial(tmp_path):
    tools = CodeTools(tmp_path)
    for index in range(257):
        (tools.root / f"entry-{index}").touch()
    report = inspect_build(tools)
    assert not report["scan_complete"] and report["project_state"] == "scan_incomplete"


@pytest.mark.parametrize("question", ["你能在本地编译吗？", "为什么无法本地编译？", "为什么说本地编译不了？",
                                      "本机能构建 iOS App 吗？", "本地能不能编译", "你能在本地编译这个项目吗？"])
def test_explicit_build_capability_questions(question):
    assert capability_question(question)


@pytest.mark.parametrize("text", ["本地编译", "在本机编译并跑测试", "为什么无法本地编译？然后修它", "请运行测试"])
def test_build_commands_and_mixed_requests_are_not_fast_questions(text):
    assert not capability_question(text)


def test_grounded_build_question_works_offline_with_busy_web_and_repl(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTO_EXEC_BACKEND", "sandbox-exec")
    monkeypatch.setattr(shutil, "which", lambda name: f"/bin/{name}" if name in {"sandbox-exec", "xcodebuild"} else None)
    model = ScriptedEngineer()
    manager = AgentManager(tmp_path, llm=model)
    agent = manager.get(manager.create("构建问询样例")["id"])
    with console(manager) as server, manager.ops_lock(agent.fleet_id), agent.dialogue_lock, agent.state_guard:
        payload = {"text": "你能在本地编译吗？"}
        assert call(server, "POST", "/api/chat", payload)[0] == 401
        status, response = call(server, "POST", "/api/chat", payload, token=server.token, agent_id=agent.fleet_id)
        assert status == 200 and response["read_only"]
        assert "xcodebuild" in response["reply"] and "没运行项目" in response["reply"]
        assert agent.chat(payload["text"]) == response["reply"]
    assert not model.messages and not agent._load_history()
    agent.chat("为什么无法本地编译？顺便解释这个任务")
    assert any("主人说：" in user and "[本机执行事实]" in user and "xcodebuild" in user for _, user in model.messages)


def test_recognizes_local_verification_without_treating_design_or_negative_as_execution():
    assert requested_checks("实现这个项目，本地编译，跑一下现有单元测试") == {"build", "test"}
    assert requested_checks("build locally; run the unit tests") == {"build", "test"}
    assert not requested_checks("为什么无法本地编译？；不要运行测试；写本地构建方案")


@pytest.mark.parametrize("argv", [["swift", "build", "--help"], ["npm", "test", "--if-present"],
                                 ["xcodebuild", "-showsdks"], ["xcodebuild", "-dry-run", "build"],
                                 ["pytest", "--collectonly"], ["python3", "-c", "print('passed')"],
                                 ["swiftc", "-typecheck", "App.swift", "-o", "bin"]])
def test_no_success_receipt_for_version_dry_run_or_printing_success(argv):
    assert command_check(argv) is None


@pytest.mark.parametrize(("result", "expected"), [
    ({"exit": -1, "reason": "executable_not_found"}, "executable_not_found"),
    ({"exit": 2, "backend": "sandbox-exec"}, "nonzero_exit"),
    ({"exit": -1, "backend": "sandbox-exec", "timeout": True}, "timeout"),
    ({"exit": 0, "backend": "subprocess"}, "not_isolated"),
    ({"exit": 0, "backend": "sandbox-exec"}, ""),
])
def test_receipt_reports_actual_status_instead_of_assuming_pytest_ran(tmp_path, result, expected):
    tools = CodeTools(tmp_path)
    receipt = verification_receipt(tools, {"argv": ["python3", "-m", "pytest", "-v"]}, result)
    assert receipt["reason"] == expected and receipt["checks"] == ["test"]


def test_task_cannot_complete_from_refusal_version_or_nonzero_exit_and_does_not_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTO_EXEC_BACKEND", "sandbox-exec")
    model = ScriptedEngineer([run_step("swift", "build", "--version"), run_step("swift", "build")])
    agent = ProtoAgent(tmp_path / "agent", llm=model)

    def simulate(argv, cwd="", timeout_s=120):
        return {"exit": 0 if "--version" in argv else 1, "backend": "sandbox-exec",
                "reason": "" if "--version" in argv else "nonzero_exit", "stderr": "DO NOT LOG COMMAND OUTPUT"}

    monkeypatch.setattr(agent.code_workspace, "run_command", simulate)
    with execution_logging(tmp_path, "test"):
        result = agent.run_task("请本地编译")
    assert result["status"] == "awaiting_retry" and result["reason"] == "local_verification_evidence_missing"
    assert "nonzero_exit" in result["missing"][0] and "无法在本地编译" not in result["summary"]
    assert "verification" not in result["trace"][0]
    assert len(model.actor_inputs) == 4 and "暂不能完成" in model.actor_inputs[-1]
    rows = (tmp_path / ".runtime-logs" / "execution.jsonl").read_text().splitlines()
    assert "DO NOT LOG" not in "\n".join(rows)
    events = [row for row in map(json.loads, rows) if row["event"] == "command_finished"]
    assert [row["exit_code"] for row in events] == [0, 1] and events[-1]["reason"] == "nonzero_exit"
    persisted = agent.sessions.lookup(result["session_id"])
    assert len([event for event in persisted["events"] if event["kind"] == "verification_retry"]) == 1


def test_probe_defaults_to_the_selected_project_and_rejects_sibling_project(tmp_path):
    model = ScriptedEngineer([tool_step("inspect_build", cwd="elsewhere"), tool_step("inspect_build")])
    agent = ProtoAgent(tmp_path, llm=model)
    agent.code_workspace.write_file("chosen/pyproject.toml", "[project]")
    agent.code_workspace.write_file("elsewhere/package.json", "{}")
    result = AgenticLoop(agent).run("查看此项目工具链", project_dir="chosen")
    assert result["status"] == "completed"
    assert "build_probe_outside_project" in "\n".join(result["observations"])
    assert len(result["trace"]) == 1 and result["trace"][0]["args"]["cwd"] == "chosen"
    assert "pyproject.toml" in result["trace"][0]["result"] and "package.json" not in result["trace"][0]["result"]


def test_passing_command_is_not_current_after_new_edits_or_from_other_project(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTO_EXEC_BACKEND", "sandbox-exec")
    model = ScriptedEngineer([run_step("pytest", cwd="other"), run_step("pytest", cwd="chosen"),
                              tool_step("write_file", path="chosen/app.py", content="value = 42\n")])
    agent = ProtoAgent(tmp_path, llm=model)
    agent.code_workspace.write_file("chosen/app.py", "value = 1\n")
    agent.code_workspace.write_file("other/app.py", "value = 4\n")
    monkeypatch.setattr(agent.code_workspace, "run_command", lambda *args: {"exit": 0, "backend": "sandbox-exec"})
    result = AgenticLoop(agent).run("运行测试", project_dir="chosen")
    assert result["reason"] == "local_verification_evidence_missing"
    model.decisions = [run_step("pytest", cwd="other")]
    result = AgenticLoop(agent).run("运行测试", project_dir="chosen")
    assert result["reason"] == "local_verification_evidence_missing"


def test_successful_verification_receipt_survives_a_safe_resume(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTO_EXEC_BACKEND", "sandbox-exec")
    model = ScriptedEngineer([run_step("pytest"), SystemExit("stopped after the executed check")])
    agent = ProtoAgent(tmp_path, llm=model)
    executed = []

    def run(argv, cwd, timeout_s):
        executed.append(argv)
        return {"exit": 0, "backend": "sandbox-exec"}

    monkeypatch.setattr(agent.code_workspace, "run_command", run)
    with pytest.raises(SystemExit):
        AgenticLoop(agent).run("运行测试")
    [session] = agent.spine.store.find("execution_sessions", lambda _: True)
    assert session["trace"][0]["verification"]["checks"] == ["test"]
    reopened = ProtoAgent(tmp_path, llm=ScriptedEngineer([{"done": True, "summary": "已运行测试"}]))
    monkeypatch.setattr(reopened.code_workspace, "run_command", run)
    result = AgenticLoop(reopened).resume(session["id"], True, safe_only=True)
    assert result["status"] == "completed" and executed == [["pytest"]]


def test_old_session_does_not_get_new_checks_at_resume(tmp_path):
    agent = ProtoAgent(tmp_path, llm=ScriptedEngineer([SystemExit("interrupted")]))
    with pytest.raises(SystemExit):
        AgenticLoop(agent).run("请本地编译")
    [session] = agent.spine.store.find("execution_sessions", lambda _: True)
    assert session.pop("local_checks") == ["build"]
    agent.sessions.save(session)
    reopened = ProtoAgent(tmp_path, llm=ScriptedEngineer([{"done": True, "summary": "旧的执行结果"}]))
    assert AgenticLoop(reopened).resume(session["id"], True, safe_only=True)["status"] == "completed"


@pytest.mark.parametrize("location", ["absolute:/tmp/FixtureApp.xcodeproj", "group:../FixtureApp.xcodeproj",
                                      "group:.hidden/FixtureApp.xcodeproj", "group:Missing.xcodeproj"])
def test_workspace_build_evidence_rejects_refs_outside_covered_sources(tmp_path, location):
    tools = CodeTools(tmp_path)
    project = "projects/demo"
    tools.write_file(f"{project}/FixtureApp.xcodeproj/project.pbxproj", "fixture")
    manifest = make_manifest(tools.root / project, location)
    arguments = {"cwd": project, "argv": ["xcodebuild", "-workspace", "Fixture.xcworkspace",
                                          "-derivedDataPath", ".proto-build-01", "build"]}
    assert ios_build_start(tools, project, arguments) is None
    make_manifest(manifest.parent.parent)
    assert ios_build_start(tools, project, arguments)["action"] == "BUILD"


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("sandbox-exec") or not shutil.which("python3"),
                    reason="real Python test requires the macOS sandbox and Python")
def test_scripted_agent_designs_implements_and_runs_real_project_test(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTO_EXEC_BACKEND", "sandbox-exec")
    decisions = [
        {"done": True, "summary": "我无法运行"},
        tool_step("read_skill", name="design-development-verification"), tool_step("inspect_build"),
        tool_step("write_file", path="DESIGN.md", content="## Acceptance\nAdd two integers, tested locally.\n"),
        tool_step("write_file", path="sample.py", content="def add(left, right):\n    return left + right\n"),
        tool_step("write_file", path="test_sample.py", content="from sample import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"),
        run_step("python3", "-m", "pytest", "-q", "test_sample.py"),
        {"done": True, "summary": "已实现并通过本机测试"},
    ]
    model = ScriptedEngineer(decisions)
    agent = ProtoAgent(tmp_path, llm=model)
    agent.code_workspace.write_file("pytest.ini", "[pytest]\n")
    if agent.code_workspace.run_command(["python3", "-m", "pytest", "--version"])["exit"] != 0:
        pytest.skip("pytest module unavailable to the sandboxed Python")
    result = AgenticLoop(agent).run("给样例实现加法，然后运行测试", acceptance="样例由本机 pytest 实际验证")
    assert result["status"] == "verified", result
    assert "工位开局探测" in model.actor_inputs[0] and "暂不能完成" in model.actor_inputs[1]
    assert result["trace"][0]["receipt"].startswith("已加载技能 design-development-verification")
    assert result["trace"][-1]["verification"]["exit"] == 0 and "1 passed" in result["trace"][-1]["result"]


@pytest.mark.skipif(sys.platform != "darwin" or not all(shutil.which(name) for name in ("sandbox-exec", "node", "npm")),
                    reason="real npm build and test require Node and the macOS sandbox")
def test_local_javascript_build_and_test_via_discovered_recipes(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTO_EXEC_BACKEND", "sandbox-exec")
    model = ScriptedEngineer()
    agent = ProtoAgent(tmp_path, llm=model)
    tools = agent.code_workspace
    tools.write_file("package.json", '{"scripts":{"build":"node build.cjs","test":"node check.cjs"}}')
    tools.write_file("app.cjs", "module.exports = 42;\n")
    tools.write_file("build.cjs", "const fs = require('node:fs');\nfs.mkdirSync('dist', {recursive: true});\n"
                     "fs.copyFileSync('app.cjs', 'dist/app.cjs');\n")
    tools.write_file("check.cjs", "require('node:assert').strictEqual(require('./dist/app.cjs'), 42);\n")
    report = inspect_build(tools)
    assert [check["argv"] for check in report["next"]] == [["npm", "run", "build"], ["npm", "run", "test"]]
    model.decisions = [tool_step("run_command", **check) for check in report["next"]] + [
        {"done": True, "summary": "npm 构建与测试均已运行"},
    ]
    result = AgenticLoop(agent).run("本地构建并运行测试", acceptance="构建产物通过 smoke")
    assert result["status"] == "verified", result
    assert [row["verification"]["checks"] for row in result["trace"]] == [["build"], ["test"]]
    assert (tools.root / "dist" / "app.cjs").is_file()


@pytest.mark.skipif(sys.platform != "darwin" or os.environ.get("PROTO_TEST_XCODE") != "1",
                    reason="opt-in actual Xcode simulator app build")
def test_agent_builds_real_ios_workspace_in_its_own_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTO_EXEC_BACKEND", "sandbox-exec")
    model = ScriptedEngineer()
    agent = ProtoAgent(tmp_path, llm=model)
    goal = agent.goal_tree.add("实现独立 iPhone App 样例", "mid", acceptance="产出可执行的模拟器应用")
    project = main_project(agent, goal)
    folder = agent.code_workspace.root / project
    shutil.copytree(Path(__file__).parent / "fixtures" / "ios", folder)
    make_manifest(folder)
    model.decisions = [
        {"done": True, "summary": "无法本地编译"},
        tool_step("read_skill", name="swift-ios-build"), tool_step("inspect_build"),
        run_step("xcodebuild", "-showsdks", cwd=project),
        run_step("xcodebuild", "-workspace", "Fixture.xcworkspace", "-list", "-disableAutomaticPackageResolution", cwd=project),
        run_step("xcodebuild", "-workspace", "Fixture.xcworkspace", "-scheme", "FixtureApp", "-destination",
                 "generic/platform=iOS Simulator", "-derivedDataPath", ".proto-build-workspace",
                 "-disableAutomaticPackageResolution", "CODE_SIGNING_ALLOWED=NO", "build", cwd=project),
        {"done": True, "summary": "模拟器应用已由本机工具构建"},
    ]
    result = agent.work_on_goal(goal)
    assert result["status"] == "verified", result
    assert "iphonesimulator" in result["trace"][2]["result"]
    assert "FixtureApp" in result["trace"][3]["result"]
    assert result["trace"][-1]["verification"]["checks"] == ["build"]
    receipt = agent.saved_ios_build(project)
    assert valid_ios_evidence(agent.code_workspace, project, receipt) and not receipt["tested"]
