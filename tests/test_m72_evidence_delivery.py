import json
import os
import plistlib
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.error import HTTPError

import pytest

from proto.exec.agentic import LOOP_SYSTEM
from proto.exec.codetools import CodeTools, _darwin_build_dirs
from proto.llm.client import AnthropicCompatClient, ModelPressureWait, OpenAICompatClient
from proto.runtime.agent import ProtoAgent
from proto.runtime.coordination import CoordinationDB, CoordinationError
from proto.runtime.manager import AgentManager
from proto.runtime.model_pressure import ModelPressure, retry_after
from proto.web.server import _activity_view
from proto.work.delivery import ios_build_result, ios_build_start, main_project, project_files, valid_ios_evidence
from proto.work.journal import GoalJournal


def http_error(hint=None):
    return HTTPError("https://provider.invalid/private", 429, "private", {} if hint is None else {"Retry-After": hint}, None)


def test_first_429_caps_two_agents_model_aliases_and_survives_reopen(tmp_path, monkeypatch):
    main = OpenAICompatClient("https://provider.invalid/v1", "secret", "large")
    fast = AnthropicCompatClient("https://provider.invalid:443/compat", "secret", "small")
    other = OpenAICompatClient("https://provider.invalid/v1", "other-key", "large")
    manager = AgentManager(tmp_path, llm=main, fast_llm=fast)
    first, second = [manager.get(manager.create(label)["id"]) for label in ("Alice", "Bob")]
    requests = []

    def blocked(request, **kwargs):
        requests.append(request.method)
        raise http_error("75")

    monkeypatch.setattr("urllib.request.urlopen", blocked)
    with pytest.raises(ModelPressureWait, match="model_rate_limited") as raised:
        first._complete(first.llm, "system", "request")
    assert 70 <= raised.value.retry_at - time.time() <= 75
    before = manager.runs.budget.view()
    own_before = second.kernel.pools.remaining_all()
    for client in (second.llm, second.llm_fast):
        with pytest.raises(ModelPressureWait, match="model_rate_limited"):
            second._complete(client, "system", "do not send", foreground=True)
    assert len(requests) == 1 and before["used"]["calls"] == 1
    assert manager.runs.budget.view() == before and second.kernel.pools.remaining_all() == own_before
    assert second.model_status()["retry_at"] == raised.value.retry_at
    assert "private" not in json.dumps(second.model_status()) and "secret" not in json.dumps(second.model_status())
    reopened = ModelPressure(CoordinationDB(tmp_path))
    assert reopened.snapshot(fast.pressure_key)["retry_at"] == raised.value.retry_at
    assert reopened.snapshot(other.pressure_key) is None


def test_retry_after_date_and_default_backoff_allow_a_single_probe(tmp_path):
    clock = [1795270000.0]
    pressure = ModelPressure(CoordinationDB(tmp_path), now=lambda: clock[0])
    hint = format_datetime(datetime.fromtimestamp(clock[0] + 90, timezone.utc), usegmt=True)
    assert retry_after(http_error(hint), clock[0]) == clock[0] + 90
    assert retry_after(http_error("tomorrow"), clock[0]) is None
    with pressure.admit("shared"):
        delay = pressure.limited("shared", http_error()).retry_at
    assert delay == clock[0] + 60
    clock[0] = delay
    with pressure.admit("shared"):
        with pytest.raises(ModelPressureWait, match="model_route_busy"):
            with pressure.admit("shared"):
                pytest.fail("second trial sent")
        with pressure.admit("independent"):
            assert pressure.snapshot("independent")["state"] == "busy"
        delay = pressure.limited("shared", http_error()).retry_at
    assert delay == clock[0] + 120
    clock[0] = delay
    with pressure.admit("shared"):
        pressure.succeeded("shared")
    assert pressure.snapshot("shared") is None


def test_foreground_gets_next_slot_and_other_routes_are_independent(tmp_path):
    pressure = ModelPressure(CoordinationDB(tmp_path), foreground_wait=2)
    entered = threading.Event()

    def foreground():
        with pressure.admit("shared", foreground=True):
            entered.set()

    worker = threading.Thread(target=foreground)
    with pressure.admit("shared"):
        worker.start()
        until = time.monotonic() + 1
        while not pressure._front.get("shared") and time.monotonic() < until:
            time.sleep(0.005)
        assert pressure._front.get("shared") == 1
        with pressure.admit("separate"):
            assert not entered.is_set()
        with pytest.raises(ModelPressureWait, match="model_route_busy"):
            with pressure.admit("shared"):
                pytest.fail("duplicate admitted")
    worker.join(3)
    assert entered.is_set()


class Scripted:
    def __init__(self, *, default=None, passed=False):
        self.decisions = []
        self.default = default or {"done": True, "summary": "完成"}
        self.passed = passed
        self.actor = 0
        self.other = []

    def complete(self, system, user):
        if system == LOOP_SYSTEM:
            self.actor += 1
            return json.dumps(self.decisions.pop(0) if self.decisions else self.default)
        self.other.append(system)
        if "独立验收" in system:
            return '{"verdict":"pass"}' if self.passed else '{"verdict":"fail","missing":["缺真实运行结果"]}'
        return "[]"


def test_max_steps_and_no_evidence_pause_across_restart(tmp_path):
    model = Scripted(default={"tool": "not_a_tool"})
    agent = ProtoAgent(tmp_path, llm=model)
    goal = agent.goal_tree.add("实现本地小工具", "mid", acceptance="必须有运行凭证")
    assert agent.work_on_goal(goal)["status"] == "max_steps"
    assert agent.work_on_goal(goal)["reason"] == "delivery_no_progress"
    assert model.actor == 48 and not model.other
    restarted = ProtoAgent(tmp_path, llm=model)
    for _ in range(15):
        assert not restarted.heartbeat_ready()
        assert restarted.maybe_proactive() is None
    assert model.actor == 48 and not model.other
    assert restarted.work_on_goal(restarted.goal_tree.get(goal.id))["status"] == "blocked"
    journal = GoalJournal(tmp_path / "work" / goal.id / "JOURNAL.md")
    assert journal.activity_view() == {"rounds": 2, "no_output": 2, "model_blocks": 0}
    restarted.retry_delivery(goal.id)
    assert restarted.heartbeat_ready()
    with pytest.raises(CoordinationError, match="delivery_not_retryable"):
        restarted.retry_delivery(goal.id)


def test_changed_files_do_not_reset_total_unverified_limit(tmp_path):
    model = Scripted()
    agent = ProtoAgent(tmp_path, llm=model)
    goal = agent.goal_tree.add("修复小工具", "mid", acceptance="需要真实结果")
    project = main_project(agent, goal)
    for number in range(4):
        model.decisions = [{"tool": "write_file", "args": {"path": f"{project}/code.py", "content": str(number)}}]
        last = agent.work_on_goal(goal)
        assert last["delivery_progress"] is True
    assert last["reason"] == "delivery_not_verified" and last["status"] == "blocked"
    assert agent.delivery_state(goal.id)["stalled"] == 0
    assert all("独立验收" in system for system in model.other)


def test_shared_workspace_and_prerequisites_leave_legacy_projects_intact(tmp_path):
    agent = ProtoAgent(tmp_path, llm=Scripted())
    root = agent.goal_tree.add("设计并实现 iPhone App", "long")
    checks = agent.goal_tree.add("本地验证与交付", "mid", parent_id=root.id, acceptance="XCTest")
    build = agent.goal_tree.add("以 SwiftUI 完整实现", "mid", parent_id=root.id, acceptance="能构建")
    design = agent.goal_tree.add("明确设计规格", "mid", parent_id=root.id, acceptance="有规格")
    prior, tests = agent._project_dir_for(build), agent._project_dir_for(checks)
    agent.code_workspace.write_file(f"{prior}/App.swift", "import SwiftUI")
    agent.code_workspace.write_file(f"{tests}/Package.swift", "// library only")
    assert main_project(agent, design) == main_project(agent, checks) == prior
    assert (agent.code_workspace.root / tests / "Package.swift").is_file()
    assert agent._available_work()[0] is design
    views = {row["goal_id"]: row for row in _activity_view(agent)["goal_work"]}
    assert views[checks.id]["waiting_for_stage"] and views[checks.id]["rounds"] == 0
    assert agent.work_on_goal(checks)["reason"] == "delivery_predecessor_pending"
    agent.goal_tree.complete(design.id)
    assert agent._available_work()[0] is build
    agent.spine.store.put("goal_delivery", build.id, {"blocked": True, "reason": "delivery_no_progress"})
    assert agent._available_work()[0] is None
    unrelated = agent.goal_tree.add("检查一个无关工具", "mid")
    assert agent._available_work()[0] is unrelated
    agent.drop_goal(build.id)
    assert (agent.code_workspace.root / prior / "App.swift").is_file()


def test_unmapped_legacy_project_is_adopted_without_renaming(tmp_path):
    agent = ProtoAgent(tmp_path, llm=Scripted())
    goal = agent.goal_tree.add("一个现有工具", "mid")
    legacy = f"projects/{agent._legacy_slug(goal.text)}"
    agent.code_workspace.write_file(f"{legacy}/start.py", "print('fixture')")
    assert main_project(agent, goal) == legacy
    assert (agent.code_workspace.root / legacy / "start.py").is_file()


def test_iphone_acceptance_requires_app_test_and_fresh_source_evidence(tmp_path, monkeypatch):
    model = Scripted(passed=True)
    agent = ProtoAgent(tmp_path, llm=model)
    root = agent.goal_tree.add("实现 iPhone App", "long")
    goal = agent.goal_tree.add("实现可运行应用", "mid", parent_id=root.id, acceptance="应用可运行")
    verify = agent.goal_tree.add("本地验证与交付", "mid", parent_id=root.id, acceptance="XCTest 通过")
    project = main_project(agent, goal)
    folder = agent.code_workspace.root / project
    agent.code_workspace.write_file(f"{project}/Package.swift", "// Just a library")
    assert agent.work_on_goal(goal)["reason"] == "ios_delivery_evidence_missing"
    assert not model.other and goal.status == "active"
    agent.code_workspace.write_file(f"{project}/App.xcodeproj/project.pbxproj", "app fixture")

    def simulate_xcode(argv, cwd, timeout_s=120):
        derived = argv[argv.index("-derivedDataPath") + 1]
        bundle = folder / derived / "Build/Products/Debug-iphonesimulator/App.app"
        bundle.mkdir(parents=True)
        (bundle / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleExecutable": "App", "CFBundlePackageType": "APPL", "CFBundleSupportedPlatforms": ["iPhoneSimulator"]}))
        (bundle / "App").write_bytes(b"\xcf\xfa\xed\xfe" + b"fixture")
        count = 0 if derived.endswith("-zero") else 1
        stdout = f"Executed {count} tests, with 0 failures\n** TEST SUCCEEDED **"
        if "build" in argv:
            stdout += "\n** BUILD SUCCEEDED **"
        return {"exit": 0, "timeout": False, "backend": "sandbox-exec", "stdout": stdout}

    monkeypatch.setattr(agent.code_workspace, "run_command", simulate_xcode)

    def xcode_step(label, action):
        return {"tool": "run_command", "args": {"cwd": project, "argv": [
            "xcodebuild", "-project", "App.xcodeproj", "-derivedDataPath", f".proto-build-{label}", action]}}

    model.decisions = [xcode_step("build", "build")]
    assert agent.work_on_goal(goal)["status"] == "verified"
    receipt = agent.saved_ios_build(project)
    assert receipt and valid_ios_evidence(agent.code_workspace, project, receipt)
    assert receipt["tested"] is False
    assert not any(path.suffix == ".plist" for path in project_files(folder))
    model.decisions = [xcode_step("tests-zero", "test")]
    assert agent.work_on_goal(verify)["reason"] == "ios_delivery_evidence_missing"
    model.decisions = [xcode_step("tests", "test")]
    assert agent.work_on_goal(verify)["status"] == "verified"
    assert agent.saved_ios_build(project)["tested"] is True
    agent.code_workspace.write_file(f"{project}/App.swift", "new source")
    assert not valid_ios_evidence(agent.code_workspace, project, receipt)


@pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("sandbox-exec"), reason="requires macOS sandbox")
def test_darwin_scratch_is_writable_but_protected_paths_and_network_are_not(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTO_EXEC_BACKEND", "sandbox-exec")
    assert {Path(path).name for path in _darwin_build_dirs()} == {"T", "C"}
    protected = tmp_path / "protected.txt"
    protected.write_text("private fixture", encoding="utf-8")
    tools = CodeTools(tmp_path / "workspace", protected_paths=[protected])
    probe = """import socket, sys, tempfile
from pathlib import Path
protected, outside, *scratch = map(Path, sys.argv[1:])
for directory in scratch:
    with tempfile.NamedTemporaryFile(dir=directory) as output:
        output.write(b'fixture')
for action in (protected.read_bytes, lambda: protected.write_bytes(b'overwrite'),
               outside.touch, lambda: socket.socket().bind(('127.0.0.1', 0))):
    try:
        action()
    except PermissionError:
        pass
    else:
        raise AssertionError('sandbox unexpectedly allowed an operation')
"""
    with tempfile.TemporaryDirectory(dir="/private/tmp") as outside:
        result = tools.run_command(["python3", "-c", probe, str(protected),
                                    str(Path(outside) / "marker"), *_darwin_build_dirs()])
    assert result["exit"] == 0, result
    assert protected.read_text(encoding="utf-8") == "private fixture"


@pytest.mark.skipif(sys.platform != "darwin" or os.environ.get("PROTO_TEST_XCODE") != "1",
                    reason="opt-in macOS Xcode simulator SDK build")
def test_real_xcode_build_evidence_from_isolated_fixture(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTO_EXEC_BACKEND", "sandbox-exec")
    tools = CodeTools(tmp_path / "workspace")
    project = "projects/fixture"
    shutil.copytree(Path(__file__).parent / "fixtures" / "ios", tools.root / project)
    argv = ["xcodebuild", "-project", "FixtureApp.xcodeproj", "-scheme", "FixtureApp", "-destination",
            "generic/platform=iOS Simulator", "-derivedDataPath", ".proto-build-fixture",
            "-disableAutomaticPackageResolution", "build"]
    start = ios_build_start(tools, project, {"argv": argv, "cwd": project})
    result = tools.run_command(argv, cwd=project, timeout_s=150)
    assert result["exit"] == 0, result
    evidence = ios_build_result(tools, project, start, result)
    assert valid_ios_evidence(tools, project, evidence)
    assert evidence["tested"] is False
    assert not ios_build_result(tools, project, start, dict(result, backend="subprocess"))
