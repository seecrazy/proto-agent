"""Read-only build discovery and small, runtime-owned verification receipts."""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path


ISOLATED = {"sandbox-exec", "runsc"}
XCODE_INPUTS = {"-project": (".xcodeproj", "project.pbxproj"),
                "-workspace": (".xcworkspace", "contents.xcworkspacedata")}
_PROGRAMS = ("xcodebuild", "swift", "swiftc", "python3", "python", "pytest", "node", "npm", "pnpm", "yarn")
_PYTHON = {"pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini", "requirements.txt"}
_OTHER = {"Cargo.toml": "rust", "go.mod": "go", "pom.xml": "maven",
          "build.gradle": "gradle", "build.gradle.kts": "gradle"}
_MARKERS = _PYTHON | set(_OTHER) | {"Package.swift", "package.json", "pnpm-lock.yaml", "yarn.lock"}
_ENGINEERING = re.compile(r"编译|构建|代码|开发|测试|沙盒|工具链|xcode|swift|pytest|\bbuild\b|\btests?\b", re.I)
_BUILD = re.compile(r"(?:本地|本机)(?:上|环境中)?(?:实际|尝试)?(?:编译|构建)|\b(?:compile|build) (?:it )?locally\b", re.I)
_TEST = re.compile(r"(?:运行|执行|跑)(?:一下|一遍|一次|一轮|下|现有|相关|全部|所有|指定的|回归|单元|集成|冒烟|本地|自动化|的|\s)*(?:测试|单测|XCTest|pytest)"
                   r"|\brun (?:the |all |existing |local |unit |integration |regression )*(?:tests?|pytest)\b", re.I)
_CAPABILITY_QUESTION = re.compile(
    r"(?:请问[，, ]*)?(?:你|本机|本地|这个环境|当前环境)?(?:为什么[说讲]?(?:你)?(?:在)?本[机地](?:不能|无法|没法)?|"
    r"(?:到底)?(?:能不能|可不可以|能否|是否支持|支持|可以|能)(?:在)?(?:本地|本机)?|(?:在)?(?:本地|本机)"
    r"(?:能不能|可不可以|能否|可以|能)?|(?:为什么)?(?:不能|无法|没法)(?:在)?(?:本机|本地)?)"
    r"\s*(?:编译|构建)(?:\s*(?:Swift|iOS(?:\s*App)?|iPhone(?:\s*App)?|App|(?:这个|当前|你的)?(?:代码|项目)))?"
    r"(?:不了|不行)?\s*(?:吗|么|呢)?[？?！!。 ]*", re.I)
_NO_CHECK = {"--help", "--version", "-help", "-version", "-h", "-V", "--dry-run", "-dry-run",
             "--collect-only", "--collectonly", "--co", "--fixtures", "--fixtures-per-test", "--setup-only", "--setup-plan",
             "--list-tests", "--listTests", "--if-present", "--showconfig", "--no-run", "--skip-build"}


def xcode_manifest(folder, flag, name):
    suffix, marker = XCODE_INPUTS[flag]
    if not name.endswith(suffix) or Path(name).name != name or len(os.fsencode(name)) > 128:
        return None
    path = folder / name / marker
    try:
        if path.resolve() == path and path.is_file():
            return path
    except (OSError, RuntimeError):
        pass
    return None


def read_manifest(path, maximum=65536):
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("not_regular_manifest")
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("manifest_too_large")
    return data


def _package_scripts(folder, markers):
    try:
        data = json.loads(read_manifest(folder / "package.json"))
        scripts = data.get("scripts", {})
        if not isinstance(scripts, dict):
            return None, []
        manager = str(data.get("packageManager", "")).split("@", 1)[0]
        if manager not in ("npm", "pnpm", "yarn"):
            manager = "pnpm" if "pnpm-lock.yaml" in markers else "yarn" if "yarn.lock" in markers else "npm"
        return manager, [name for name in ("build", "test")
                         if isinstance(scripts.get(name), str) and scripts[name].strip()]
    except (OSError, ValueError, AttributeError, RecursionError):
        return None, []


_SWIFT_TARGET = re.compile(r'\.(target|testTarget)\(\s*name:\s*"([^"]+)"[^)]*?path:\s*"([^"]+)"', re.S)
_SWIFT_MAIN = re.compile(r"^\s*@main\b", re.M)


def _package_structure_hint(folder, manifest):
    """带 @main 的 App 入口和被测代码放在同一个 library target：`swift test` 链接时
    duplicate symbol '_main'（2026-11-24 生产实证：专家每片都先撞这一步）。"""
    targets = _SWIFT_TARGET.findall(manifest or "")
    if not any(kind == "testTarget" for kind, _, _ in targets):
        return ""
    for kind, name, path in targets:
        if kind != "target":
            continue
        try:
            source = (folder / path).resolve()
            source.relative_to(Path(folder).resolve())
        except (OSError, ValueError, RuntimeError):
            continue
        if not source.is_dir():
            continue
        for index, item in enumerate(sorted(source.rglob("*.swift"))):
            if index >= 32 or any(part.startswith(".") for part in item.relative_to(source).parts):
                continue
            try:
                if _SWIFT_MAIN.search(item.read_text(encoding="utf-8", errors="replace")[:4000]):
                    return (f"Swift 包里带 @main 的入口和被测代码都在 target「{name}」：`swift test` 链接时会 "
                            "duplicate symbol '_main'。iOS 应用以生成的 .xcodeproj 跑 `xcodebuild test` 为准"
                            "（测试 target 由应用托管）；要保留 swift test，就把 @main 入口拆成单独的 executable target。")
            except OSError:
                continue
    return ""


def inspect_build(workspace, cwd=""):
    report = workspace.command_environment(_PROGRAMS)
    report.update(cwd=cwd or ".", project_state="directory_unavailable", inputs=[], next=[], hints=[],
                  scan_complete=True)
    try:
        folder = workspace._abs(cwd).resolve()
        if not folder.is_dir():
            return report
        relative = folder.relative_to(workspace.root.resolve()).as_posix()
        markers = set()
        xcode = []
        with os.scandir(folder) as entries:
            for index, entry in enumerate(entries):
                if index >= 256:
                    report["scan_complete"] = False
                    break
                if entry.name in _MARKERS and entry.is_file(follow_symlinks=False):
                    markers.add(entry.name)
                for flag, (suffix, _marker) in XCODE_INPUTS.items():
                    if entry.name.endswith(suffix) and xcode_manifest(folder, flag, entry.name):
                        if len(xcode) < 8:
                            xcode.append((flag, entry.name))
                        else:
                            report["scan_complete"] = False
    except (OSError, ValueError, RuntimeError):
        return report

    report["cwd"] = relative
    report["project_state"] = "unrecognized" if report["scan_complete"] else "scan_incomplete"
    programs = report["programs"]

    def add_check(*argv):
        if report["runner"] != "unavailable" and programs.get(argv[0]) and len(report["next"]) < 7:
            report["next"].append({"argv": list(argv), "cwd": relative})

    if xcode:
        report["inputs"].extend({"kind": "xcode", "flag": flag, "path": name} for flag, name in sorted(xcode))
        add_check("xcodebuild", "-version")
        add_check("xcodebuild", "-showsdks")
        if len(xcode) == 1 and report["scan_complete"]:
            flag, name = xcode[0]
            add_check("xcodebuild", flag, name, "-list", "-disableAutomaticPackageResolution")
    if "Package.swift" in markers:
        report["inputs"].append({"kind": "swift-package", "path": "Package.swift"})
        add_check("swift", "build")
        add_check("swift", "test")
        if not xcode:
            # M82：Swift 包能编译不等于拿到 iPhone App；交付门要的是 .xcodeproj 的模拟器回执
            report["hints"].append("工位只有 Swift Package、没有 Xcode 工程：iOS 应用交付需要 .xcodeproj——"
                                   "read_skill ios-app-project 按说明生成")
        try:
            structure = _package_structure_hint(folder, (folder / "Package.swift").read_text(
                encoding="utf-8", errors="replace")[:65536])
        except OSError:
            structure = ""
        if structure:
            report["hints"].append(structure)
    if markers & _PYTHON:
        report["inputs"].append({"kind": "python", "paths": sorted(markers & _PYTHON)})
        interpreter = next((name for name in ("python3", "python") if programs[name]), None)
        if interpreter:
            add_check(interpreter, "-m", "pytest", "-q")
            if markers & {"pyproject.toml", "setup.py"}:
                add_check(interpreter, "-m", "build", "--no-isolation")
        else:
            add_check("pytest", "-q")
    if "package.json" in markers:
        manager, scripts = _package_scripts(folder, markers)
        report["inputs"].append({"kind": "javascript", "path": "package.json", "manager": manager, "scripts": scripts})
        for script in scripts:
            add_check(manager, "run", script)
    for name in sorted(markers & set(_OTHER)):
        report["inputs"].append({"kind": _OTHER[name], "path": name, "reason": "no_registered_recipe"})
    if report["inputs"]:
        report["project_state"] = "discovered"
    return report


def engineering_topic(text):
    return _ENGINEERING.search(text) is not None


def capability_question(text):
    return (re.search(r"[吗么？?]|为什么|能否|能不能|可不可以|是否", text) is not None
            and _CAPABILITY_QUESTION.fullmatch(text.strip()) is not None)


def capability_reply(workspace):
    facts = workspace.command_environment(_PROGRAMS)
    available = [name for name, found in facts["programs"].items() if found]
    programs = "、".join(available) or "未找到已登记的本机工具"
    runner = {"isolated": f"支持通过 {facts['backend']} 沙盒在项目工位尝试",
              "owner_approval": "仅启用了非隔离执行，必须先由主人审批",
              "unavailable": "当前没有可用的命令执行后端"}[facts["runner"]]
    return (f"{runner}。当前 PATH：{programs}。"
            "这次只查了执行器和工具是否在 PATH，没运行项目。"
            "是否有对应工程、SDK、依赖及能否编译/通过测试，要看工位和实际命令回执。")


def requested_checks(task_text):
    requirements = set()
    for phrase in re.split(r"[。！!？?；;，,\n]", task_text.split("\n[", 1)[0]):
        if (capability_question(phrase) or re.search(r"不要|无需|不必|不用|不能|无法|不需要|为什么|能否|能不能|是否|"
                                                    r"\b(?:do not|don't|without|can you|why)\b", phrase, re.I)):
            continue
        if _BUILD.search(phrase) and not re.search(r"(?:编译|构建)(?:的)?(?:方案|设计|说明|教程)", phrase):
            requirements.add("build")
        if _TEST.search(phrase) and not re.search(r"(?:测试|单测)(?:的)?(?:方案|设计|说明|教程)", phrase):
            requirements.add("test")
    return requirements


def command_check(argv):
    if not argv or any(token.split("=", 1)[0] in _NO_CHECK for token in argv[1:]):
        return None
    command, *args = argv
    program = Path(command).name
    if program == "xcrun" and args[:1] == ["xcodebuild"]:
        program, args = "xcodebuild", args[1:]
    if (program in ("python3", "python") and args[:2] == ["-m", "pytest"]) or program == "pytest":
        return "test", ""
    if program in ("python3", "python") and args[:2] == ["-m", "build"]:
        return "build", ""
    if (program == "swift" and args[:1] == ["test"]) or (
            program in ("npm", "pnpm", "yarn") and args in (["run", "test"], ["test"])):
        return "test", ""
    if (program == "swift" and args[:1] == ["build"]) or (
            program in ("npm", "pnpm", "yarn") and args == ["run", "build"]):
        return "build", ""
    if (program == "swiftc" and any(arg.endswith(".swift") for arg in args)
            and "-o" in args and not any(arg in ("-parse", "-typecheck", "-dump-ast", "-print-target-info") for arg in args)):
        return "build", ""
    if program == "xcodebuild" and not any(arg in args for arg in ("-list", "-showsdks", "-showBuildSettings")):
        actions = set(args) & {"test", "build", "build-for-testing"}
        if actions == {"test"}:
            return "test", "TEST"
        if actions in ({"build"}, {"build-for-testing"}):
            return "build", "BUILD" if "build" in actions else "TEST BUILD"
    return None


def verification_receipt(workspace, arguments, result):
    check = command_check(arguments.get("argv", []))
    if check is None or not isinstance(result, dict) or type(result.get("exit")) is not int:
        return None
    try:
        cwd = workspace._abs(arguments.get("cwd", "")).resolve().relative_to(workspace.root.resolve())
    except (OSError, ValueError, RuntimeError):
        return None
    kind, xcode_action = check
    stdout = result.get("stdout", "")
    reason = result.get("reason", "")
    if result.get("timeout"):
        reason = "timeout"
    elif not reason and result["exit"] != 0:
        reason = "nonzero_exit"
    elif not reason and result.get("backend") not in ISOLATED:
        reason = "not_isolated"
    elif not reason and xcode_action and (
            f"** {xcode_action} SUCCEEDED **" not in stdout.splitlines() or (xcode_action == "TEST" and not re.search(
                r"Executed [1-9][0-9]* tests?[, ]|Test run with [1-9][0-9]* tests?", stdout))):
        reason = "xcode_confirmation_missing"
    checks = ["build", "test"] if kind == "test" and xcode_action else [kind]
    return {"checks": checks, "cwd": cwd.as_posix(), "exit": result["exit"],
            "backend": result.get("backend", ""), "reason": reason}


def missing_checks(required, trace, directory):
    remaining = set(required)
    if not remaining:
        return []
    reasons = {}
    for row in reversed(trace):
        if row.get("written_path"):
            break
        receipt = row.get("verification")
        if (not isinstance(receipt, dict) or not isinstance(receipt.get("cwd"), str)
                or not isinstance(receipt.get("checks"), list) or not isinstance(receipt.get("reason"), str)):
            continue
        if not Path(receipt["cwd"]).is_relative_to(Path(directory or ".")):
            continue
        matching = {check for check in remaining if check in receipt["checks"] and check not in reasons}
        if receipt["reason"]:
            reasons.update({check: receipt["reason"] for check in matching})
        else:
            remaining.difference_update(matching)
        if not remaining:
            break
    labels = {"build": "本地构建", "test": "实际运行测试"}
    return [f"{labels[check]}缺少同工位的有效命令回执（{reasons.get(check, 'not_verified')}）"
            for check in sorted(remaining)]
