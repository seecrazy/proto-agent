# -*- coding: utf-8 -*-
"""M82：ios-app-project——专家自己生成 .xcodeproj，iOS 交付在结构上可达。

生产实证：交付门要求 `xcodebuild -project X.xcodeproj …` 产出模拟器 .app，但系统里
没有任何办法生成 .xcodeproj（测试用的是手工夹具），验收条款从未可达。
"""
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from importlib.resources import files
from pathlib import Path

import pytest

from proto.exec.engineering import inspect_build
from proto.skills import catalog, read_skill
from proto.tools.codetools import CodeTools

SCRIPT = Path(__file__).resolve().parents[1] / "proto" / "skills" / "ios-app-project" / "make_xcodeproj.py"
PLUTIL = shutil.which("plutil")


@pytest.fixture(scope="module")
def generator():
    spec = importlib.util.spec_from_file_location("make_xcodeproj", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_app(root):
    (root / "Sources/Wengu").mkdir(parents=True, exist_ok=True)
    (root / "Tests/WenguTests").mkdir(parents=True, exist_ok=True)
    (root / "Notes.md").write_text("# notes\n", encoding="utf-8")
    (root / "Sources/Wengu/WenguApp.swift").write_text(
        'import SwiftUI\n\n@main\nstruct WenguApp: App {\n'
        '    var body: some Scene { WindowGroup { Text("Wengu") } }\n}\n', encoding="utf-8")
    (root / "Sources/Wengu/Models.swift").write_text("struct Card {\n    var front: String\n}\n", encoding="utf-8")
    (root / "Tests/WenguTests/CardTests.swift").write_text(
        "import XCTest\n@testable import Wengu\n\nfinal class CardTests: XCTestCase {\n"
        "    func testFront() { XCTAssertEqual(Card(front: \"a\").front, \"a\") }\n}\n", encoding="utf-8")
    return {"name": "Wengu", "bundle_id": "com.sanshi.wengu", "sources": ["Sources/Wengu"],
            "tests": {"name": "WenguTests", "bundle_id": "com.sanshi.wengu.tests",
                      "sources": ["Tests/WenguTests"]}}


def with_ui_tests(root, spec):
    """M85：验收里的「UI 测试代码证明主流程」此前结构上不可达——只有 app+单元测试
    两种 target。加 UI 测试 target（XCUITest）后模拟器内点按走查才生成得了。"""
    (root / "UITests/WenguUITests").mkdir(parents=True, exist_ok=True)
    (root / "UITests/WenguUITests/SmokeUITests.swift").write_text(
        "import XCTest\n\nfinal class SmokeUITests: XCTestCase {\n"
        "    func testLaunch() {\n"
        "        let app = XCUIApplication()\n        app.launch()\n"
        "        XCTAssertTrue(app.exists)\n    }\n}\n", encoding="utf-8")
    spec["ui_tests"] = {"name": "WenguUITests", "bundle_id": "com.sanshi.wengu.uitests",
                        "sources": ["UITests/WenguUITests"]}
    return spec


def generate(generator, root, spec, name="project.json"):
    (root / name).write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    return generator.generate(name, root)


def lint(path):
    if not PLUTIL:
        return
    check = subprocess.run([PLUTIL, "-lint", str(path)], capture_output=True, text=True, timeout=60)
    assert check.returncode == 0, check.stdout + check.stderr


def test_generated_project_is_stable_and_well_formed(tmp_path, generator):
    generate(generator, tmp_path, make_app(tmp_path))
    pbx = tmp_path / "Wengu.xcodeproj/project.pbxproj"
    scheme = tmp_path / "Wengu.xcodeproj/xcshareddata/xcschemes/Wengu.xcscheme"
    before = (pbx.read_bytes(), scheme.read_bytes())
    summary = generate(generator, tmp_path, make_app(tmp_path))
    assert (pbx.read_bytes(), scheme.read_bytes()) == before          # 重复生成字节一致
    assert (summary["app_sources"], summary["test_sources"]) == (2, 1) and summary["scheme"] == "Wengu"
    text = pbx.read_text(encoding="utf-8")
    assert text.startswith("// !$*UTF8*$!") and "objectVersion = 56" in text
    assert "com.apple.product-type.application" in text
    assert "com.apple.product-type.bundle.unit-test" in text
    assert "$(BUILT_PRODUCTS_DIR)/Wengu.app/$(BUNDLE_EXECUTABLE_FOLDER_PATH)/Wengu" in text
    assert "GENERATE_INFOPLIST_FILE" in text and "com.sanshi.wengu.tests" in text
    assert text.count("PBXNativeTarget") >= 3 and "PBXTargetDependency" in text
    lint(pbx)
    assert ET.parse(scheme).getroot().tag == "Scheme" and "WenguTests.xctest" in scheme.read_text(encoding="utf-8")


@pytest.mark.skipif(not shutil.which("xcodebuild"), reason="需要 Xcode")
def test_xcodebuild_lists_the_generated_project(tmp_path, generator):
    generate(generator, tmp_path, make_app(tmp_path))
    listing = subprocess.run(["xcodebuild", "-list", "-project", "Wengu.xcodeproj"], cwd=tmp_path,
                             capture_output=True, text=True, timeout=180)
    assert listing.returncode == 0, listing.stdout + listing.stderr
    assert "WenguTests" in listing.stdout and "Wengu" in listing.stdout and "Debug" in listing.stdout


def test_groups_resources_info_and_settings(tmp_path, generator):
    spec = make_app(tmp_path)
    (tmp_path / "Resources/Assets.xcassets").mkdir(parents=True)
    (tmp_path / "Resources/Assets.xcassets/Contents.json").write_text('{"info":{"author":"xcode","version":1}}')
    spec.update(resources=["Resources/Assets.xcassets"],
                info={"UISupportedInterfaceOrientations": ["UIInterfaceOrientationPortrait"]},
                settings={"SWIFT_STRICT_CONCURRENCY": "minimal", "Debug": {"ONLY_ACTIVE_ARCH": "YES"}},
                device_family="1")
    generate(generator, tmp_path, spec)
    text = (tmp_path / "Wengu.xcodeproj/project.pbxproj").read_text(encoding="utf-8")
    assert "lastKnownFileType = folder.assetcatalog" in text and "in Resources" in text
    assert 'INFOPLIST_KEY_UISupportedInterfaceOrientations = "UIInterfaceOrientationPortrait";' in text
    assert 'SWIFT_STRICT_CONCURRENCY = "minimal";' in text and "ONLY_ACTIVE_ARCH = YES;" in text
    assert 'TARGETED_DEVICE_FAMILY = "1";' in text and 'path = Resources;' in text
    lint(tmp_path / "Wengu.xcodeproj/project.pbxproj")


def test_quotes_paths_with_spaces_and_non_ascii(tmp_path, generator):
    spec = make_app(tmp_path)
    (tmp_path / "Sources/Wengu/我的 View.swift").write_text("struct View1 {}\n", encoding="utf-8")
    spec["name"] = "Wengu"
    summary = generate(generator, tmp_path, spec)
    text = (tmp_path / "Wengu.xcodeproj/project.pbxproj").read_text(encoding="utf-8")
    assert summary["app_sources"] == 3 and '"我的 View.swift"' in text
    lint(tmp_path / "Wengu.xcodeproj/project.pbxproj")


@pytest.mark.parametrize("change, message", [
    ({"sources": ["../elsewhere"]}, "inside the project"),
    ({"sources": ["missing"]}, "inside the project"),
    ({"sources": []}, "no compilable files"),
    ({"name": "My App"}, "name must"),
    ({"bundle_id": "wengu"}, "bundle_id"),
    ({"device_family": "3"}, "device_family"),
    ({"deployment_target": "seventeen"}, "17.0"),
    ({"info": {"UI-Supported": 1}}, "info keys"),
    ({"tests": {"name": "Wengu", "bundle_id": "a.b.c", "sources": ["Tests/WenguTests"]}}, "tests.name"),
    ({"tests": {"name": "WenguTests", "bundle_id": "com.sanshi.wengu", "sources": ["Tests/WenguTests"]}},
     "tests.bundle_id"),
    ({"tests": {"name": "WenguTests", "bundle_id": "a.b.tests", "sources": ["Notes.md"]}}, "does not take"),
    ({"resources": ["Sources/Wengu/Models.swift"]}, "does not take"),
    ({"sources": "Sources/Wengu"}, "array of paths"),
    ({"ui_tests": {"name": "WenguTests", "bundle_id": "a.b.uitests", "sources": ["UITests/WenguUITests"]}},
     "ui_tests.name"),
    ({"ui_tests": {"name": "WenguUITests", "bundle_id": "com.sanshi.wengu", "sources": ["UITests/WenguUITests"]}},
     "ui_tests.bundle_id"),
    ({"ui_tests": {"name": "WenguUITests", "bundle_id": "a.b.uitests", "sources": ["Notes.md"]}}, "does not take"),
])
def test_spec_rejects_bad_inputs(tmp_path, generator, change, message):
    spec = with_ui_tests(tmp_path, make_app(tmp_path)) if "ui_tests" in change else make_app(tmp_path)
    spec.update(change)
    with pytest.raises(generator.SpecError, match=message):
        generate(generator, tmp_path, spec)


def test_ui_test_target_is_generated_and_stable(tmp_path, generator):
    generate(generator, tmp_path, with_ui_tests(tmp_path, make_app(tmp_path)))
    pbx = tmp_path / "Wengu.xcodeproj/project.pbxproj"
    xscheme = tmp_path / "Wengu.xcodeproj/xcshareddata/xcschemes/Wengu.xcscheme"
    before = (pbx.read_bytes(), xscheme.read_bytes())
    summary = generate(generator, tmp_path, with_ui_tests(tmp_path, make_app(tmp_path)))
    assert (pbx.read_bytes(), xscheme.read_bytes()) == before          # 重复生成字节一致
    assert summary["ui_test_sources"] == 1 and summary["test_sources"] == 1
    text = pbx.read_text(encoding="utf-8")
    assert "com.apple.product-type.bundle.ui-testing" in text
    assert "WenguUITests.xctest" in text and "com.sanshi.wengu.uitests" in text
    assert "TEST_TARGET_NAME = Wengu;" in text                          # UI 测试指回被测 App
    assert text.count("PBXTargetDependency") >= 2                        # 两个测试 target 都依赖 App
    assert "TEST_HOST" in text                                           # 单元测试注入式宿主仍在
    lint(pbx)
    scheme_text = xscheme.read_text(encoding="utf-8")
    assert scheme_text.count("TestableReference skipped = \"NO\"") == 2   # 单测 + UI 测试都进 Test 动作
    assert "WenguTests.xctest" in scheme_text and "WenguUITests.xctest" in scheme_text


@pytest.mark.skipif(not shutil.which("xcodebuild"), reason="需要 Xcode")
def test_xcodebuild_lists_ui_test_target(tmp_path, generator):
    generate(generator, tmp_path, with_ui_tests(tmp_path, make_app(tmp_path)))
    listing = subprocess.run(["xcodebuild", "-list", "-project", "Wengu.xcodeproj"], cwd=tmp_path,
                             capture_output=True, text=True, timeout=180)
    assert listing.returncode == 0, listing.stdout + listing.stderr
    assert "WenguUITests" in listing.stdout and "WenguTests" in listing.stdout


def test_main_reports_spec_errors_with_exit_code_2(tmp_path, generator, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "bad.json").write_text("{\"name\": \"1nvalid\"}", encoding="utf-8")
    assert generator.main(["bad.json"]) == 2
    assert "name must" in capsys.readouterr().err
    good = tmp_path / "app"
    good.mkdir()
    make_app(good)
    (good / "project.json").write_text(json.dumps(make_app(good)), encoding="utf-8")
    monkeypatch.chdir(good)
    assert generator.main(["project.json"]) == 0
    assert json.loads(capsys.readouterr().out)["project"] == "Wengu.xcodeproj"


def test_skill_is_listed_with_its_script():
    assert "ios-app-project" in catalog()
    skill = read_skill("ios-app-project")
    assert skill["description"] and ".xcodeproj" in skill["instructions"]
    script = Path(skill["scripts"]["make_xcodeproj"])
    assert script.is_file() and script.name == "make_xcodeproj.py" and ".xcodeproj" in skill["instructions"]
    assert read_skill("local-verification")["scripts"] == {}
    data = files("proto.skills").joinpath("ios-app-project").joinpath("SKILL.md").read_bytes()
    assert len(data) <= 2500 and skill["sha256"] == hashlib.sha256(data).hexdigest()


def test_inspect_build_hints_when_only_swift_package(tmp_path):
    tools = CodeTools(tmp_path)
    tools.write_file("p/Package.swift", "// swift-tools-version:5.9")
    report = inspect_build(tools, "p")
    assert [hint for hint in report["hints"] if "ios-app-project" in hint]
    tools.write_file("q/README.md", "x")
    assert inspect_build(tools, "q")["hints"] == []


def test_delivery_instructions_point_to_project_skill_and_real_devices():
    from proto.runtime import agent as runtime_agent
    text = runtime_agent.IOS_DELIVERY_INSTRUCTIONS
    assert "ios-app-project" in text and "simctl list devices available" in text
    source = Path(runtime_agent.__file__).read_text(encoding="utf-8")
    assert source.count("IOS_DELIVERY_INSTRUCTIONS") >= 2          # 定义之外确实被交付阶段用到


@pytest.mark.skipif(sys.platform != "darwin" or os.environ.get("PROTO_TEST_XCODE") != "1",
                    reason="opt-in：真实 Xcode 模拟器构建")
def test_generated_project_builds_in_the_proto_sandbox(tmp_path, generator, monkeypatch):
    monkeypatch.setenv("PROTO_EXEC_BACKEND", "sandbox-exec")
    app = tmp_path / "ws/projects/wengu"
    app.mkdir(parents=True)
    make_app(app)
    generate(generator, app, make_app(app))
    tools = CodeTools(tmp_path / "ws")
    built = tools.run_command(["xcodebuild", "-project", "Wengu.xcodeproj", "-scheme", "Wengu", "-destination",
                               "generic/platform=iOS Simulator", "-derivedDataPath", ".proto-build-01",
                               "-disableAutomaticPackageResolution", "CODE_SIGNING_ALLOWED=NO", "build"],
                              cwd="projects/wengu", timeout_s=300)
    assert built["exit"] == 0, built["stdout"][-2000:] + built["stderr"][-2000:]
    assert list(app.glob(".proto-build-01/Build/Products/*-iphonesimulator/*.app"))
