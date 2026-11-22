"""Progress, project identity and locally checkable evidence for autonomous work."""
from __future__ import annotations

import hashlib
import os
import plistlib
import re
import stat
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

from proto.exec.engineering import XCODE_INPUTS, command_check, read_manifest, xcode_manifest
from proto.runtime.activity import artifact_path


MAX_STALLED_SLICES = 2
MAX_UNVERIFIED_SLICES = 4
_GENERATED = {"node_modules", "__pycache__", "build", "DerivedData", "dist"}
_APP = re.compile(r"(?:iphone|ios|swiftui)", re.I)
_DESIGN = re.compile(r"规格|设计|概念|spec(?:ification)?|design", re.I)
_IMPLEMENT = re.compile(r"实现|编码|开发|implement|build", re.I)
_CHECK = re.compile(r"本地验证|验证|测试|交付|xctest|test|deliver", re.I)
_PROJECT_COLLECTIONS = ("delivery_projects", "project_dirs")


class ProjectDirectoryError(RuntimeError):
    """The recorded project directory cannot safely be used."""


def ios_stage(root, node):
    if (not _APP.search(root.text) or not _IMPLEMENT.search(root.text)
            or not re.search(r"\bapp\b|应用", root.text, re.I)):
        return ""
    if node is root:
        return "" if root.level == "long" else "build"
    text = re.split(r"[:：，、；。]", node.text, 1)[0][:40]
    if _DESIGN.search(text) and not _IMPLEMENT.search(text):
        return "design"
    if _CHECK.search(text) and not _IMPLEMENT.search(text):
        return "verify"
    return "build"


def stage_waiters(tree, active):
    ranks = {"design": 1, "build": 2, "verify": 3}
    groups = {}
    excluded = {node.parent_id for node in active if node.parent_id}
    for node in active:
        root = tree.root_for(node)
        if root is not None and root is not node:
            rank = ranks.get(ios_stage(root, node))
            if rank:
                groups.setdefault(root.id, []).append((rank, node.id))
    for group in groups.values():
        first = min(rank for rank, _goal in group)
        excluded.update(goal for rank, goal in group if rank > first)
    return excluded


def _directory_name(directory):
    directory = artifact_path(directory)
    if not directory:
        return None
    item = PurePosixPath(directory)
    if len(item.parts) != 2 or item.name.startswith((".", "_")) or item.as_posix() != directory:
        return None
    return item.name


def project_path(root, directory):
    if not _directory_name(directory):
        return None
    try:
        projects = root / "projects"
        path = root / directory
        if (projects.exists() and not projects.is_dir()) or path.resolve() != path:
            return None
        return path if not path.exists() or path.is_dir() else None
    except (OSError, RuntimeError, ValueError):
        return None


def project_files(folder):
    if folder.is_symlink():
        return
    for current, dirs, names in os.walk(folder, followlinks=False):
        dirs[:] = [name for name in dirs if not name.startswith((".", "_")) and name not in _GENERATED]
        for name in names:
            path = Path(current) / name
            if not name.startswith(".") and not path.is_symlink() and path.is_file():
                yield path


def project_slug(text):
    normalized = unicodedata.normalize("NFKD", text)
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    title = re.split(r"[:\r\n]", normalized, maxsplit=1)[0]
    words = re.findall(r"[a-z0-9]+", title.lower()) or re.findall(r"[a-z0-9]+", normalized.lower())
    parts = []
    length = 0
    for word in dict.fromkeys(words):
        if length + len(word) + bool(parts) > 24:
            if parts:
                break
            continue
        length += len(word) + bool(parts)
        parts.append(word)
    return "-".join(parts) or "goal"


def project_bindings(agent):
    store = agent.spine.store
    for goal in agent.goal_tree.snapshot()["nodes"]:
        for collection in _PROJECT_COLLECTIONS:
            directory = (store.get(collection, goal["id"]) or {}).get("dir")
            if _directory_name(directory):
                yield goal, directory, collection


def _allocate_project(work_root, root, claimed):
    slug = project_slug(root.text)
    short = f"{slug}-{root.id[:8]}"
    complete = f"{slug}-{root.id}"
    projects = work_root / "projects"
    if projects.is_symlink() or (projects.exists() and not projects.is_dir()):
        raise ProjectDirectoryError("projects")
    taken = {PurePosixPath(directory).name.casefold() for directory in claimed}
    if projects.is_dir():
        taken.update(path.name.casefold() for path in projects.iterdir())
    name = complete if short.casefold() in taken else short
    number = 2
    while name.casefold() in taken:
        name = f"{complete}-{number}"
        number += 1
    return f"projects/{name}"


def main_project(agent, node):
    root = agent.goal_tree.root_for(node) or node
    store = agent.spine.store
    work_root = agent.code_workspace.root.resolve()
    saved = store.get("delivery_projects", root.id)
    if saved is not None:
        directory = saved.get("dir")
        if project_path(work_root, directory) is None:
            raise ProjectDirectoryError(directory)
        return directory
    lineage = (root, *agent.goal_tree.related_goals(root.id))
    lineage_ids = {related.id for related in lineage}
    claimed = {directory.casefold() for goal, directory, _collection in project_bindings(agent)
               if goal["id"] not in lineage_ids}
    candidates = []
    original = None
    unavailable = None
    for related in lineage:
        directory = (store.get("project_dirs", related.id) or {}).get("dir")
        if not directory:
            continue
        folder = project_path(work_root, directory)
        if folder is None or directory.casefold() in claimed:
            unavailable = directory
            continue
        if related is root:
            original = directory
        if not folder.is_dir():
            continue
        rank = (any(folder.glob("*.xcodeproj/project.pbxproj"))
                or any(folder.glob("*.xcworkspace/contents.xcworkspacedata")),
                ios_stage(root, related) == "build", (folder / "Package.swift").is_file(),
                related is root, -related.created_at)
        candidates.append((rank, directory))
    if candidates:
        directory = max(candidates)[1]
    elif original:
        directory = original
    elif unavailable:
        raise ProjectDirectoryError(unavailable)
    else:
        directory = _allocate_project(work_root, root, claimed)
        if project_path(work_root, directory) is None:
            raise ProjectDirectoryError(directory)
        store.put("project_dirs", root.id, {"dir": directory, "text": root.text[:80]})
    store.put("delivery_projects", root.id, {"dir": directory})
    return directory


def snapshot(workspace, project_dir, *, sources=False):
    folder = project_path(workspace.root.resolve(), project_dir)
    if folder is None:
        return None
    digest = hashlib.sha256()
    remaining = 32 * 1024 * 1024
    count = 0
    pending = [folder]
    try:
        if not folder.exists():
            return digest.hexdigest()
        while pending:
            directory = pending.pop()
            rows = []
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name.startswith(".") or entry.name in _GENERATED:
                        continue
                    count += 1
                    if count > 2048:
                        return None
                    rows.append(entry)
            for entry in sorted(rows, key=lambda item: item.name):
                if entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                    continue
                if sources and entry.name.lower().endswith(('.md', '.markdown')):
                    continue
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    continue
                if info.st_size > remaining:
                    return None
                with open(entry.path, "rb") as stream:
                    content = stream.read(info.st_size + 1)
                if len(content) != info.st_size:
                    return None
                remaining -= len(content)
                digest.update(os.fsencode(Path(entry.path).relative_to(folder)) + b"\0")
                digest.update(hashlib.sha256(content).digest())
    except (OSError, ValueError):
        return None
    return digest.hexdigest()


def _value(argv, flag):
    if argv.count(flag) != 1:
        return ""
    index = argv.index(flag) + 1
    return argv[index] if index < len(argv) else ""


def _app_binary(bundle):
    try:
        if bundle.resolve() != bundle:
            return None
        info = bundle / "Info.plist"
        if info.is_symlink() or not info.is_file() or info.stat().st_size > 128_000:
            return None
        metadata = plistlib.loads(info.read_bytes())
        name = metadata.get("CFBundleExecutable")
        if (metadata.get("CFBundlePackageType") != "APPL" or not isinstance(name, str)
                or not name or Path(name).name != name
                or "iPhoneSimulator" not in metadata.get("CFBundleSupportedPlatforms", [])):
            return None
        binary = bundle / name
        if not binary.is_file() or binary.resolve() != binary:
            return None
        with binary.open("rb") as stream:
            magic = stream.read(4)
        if magic in (b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):
            return binary
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError):
        return None
    return None


def _local_xcode_workspace(folder, manifest):
    try:
        data = read_manifest(manifest).decode("utf-8")
        if "<!DOCTYPE" in data.upper():
            return False
        root = ET.fromstring(data)
        if root.tag != "Workspace":
            return False
        todo = [(folder, item) for item in root]
        found = count = 0
        while todo:
            parent, item = todo.pop()
            count += 1
            kind, _, location = item.attrib.get("location", "").partition(":")
            relative = Path(location)
            if (count > 128 or kind not in ("group", "container") or relative.is_absolute()
                    or any(part == ".." or part.startswith(".") or part in _GENERATED for part in relative.parts)):
                return False
            target = (parent if kind == "group" else folder) / relative
            if target.resolve() != target:
                return False
            if item.tag == "Group":
                todo.extend((target, child) for child in item)
            elif item.tag == "FileRef" and xcode_manifest(target.parent, "-project", target.name):
                found += 1
            else:
                return False
        return found > 0
    except (ET.ParseError, OSError, ValueError, RuntimeError):
        return False


def ios_build_start(workspace, project_dir, arguments):
    argv = arguments.get("argv", [])
    check = command_check(argv)
    if not check or not check[1] or arguments.get("cwd") != project_dir:
        return None
    derived = _value(argv, "-derivedDataPath")
    options = [flag for flag in XCODE_INPUTS if flag in argv]
    if len(options) != 1 or not re.fullmatch(r"\.proto-build(?:-[A-Za-z0-9_-]{1,40})?", derived):
        return None
    folder = project_path(workspace.root.resolve(), project_dir)
    if folder is None or (folder / derived).exists() or (folder / derived).is_symlink():
        return None
    flag = options[0]
    manifest = xcode_manifest(folder, flag, _value(argv, flag))
    if manifest is None or (flag == "-workspace" and not _local_xcode_workspace(folder, manifest)):
        return None
    digest = snapshot(workspace, project_dir, sources=True)
    return {"sources": digest, "derived": derived, "action": check[1]} if digest else None


def ios_build_result(workspace, project_dir, start, result):
    if not (isinstance(start, dict) and isinstance(start.get("sources"), str)
            and isinstance(start.get("derived"), str) and re.fullmatch(r"\.proto-build(?:-[A-Za-z0-9_-]{1,40})?", start["derived"])
            and isinstance(result, dict) and result.get("backend") == "sandbox-exec"
            and result.get("exit") == 0 and not result.get("timeout")):
        return None
    stdout = str(result.get("stdout", ""))
    action = start.get("action")
    if action not in ("BUILD", "TEST", "TEST BUILD") or f"** {action} SUCCEEDED **" not in stdout.splitlines():
        return None
    if snapshot(workspace, project_dir, sources=True) != start["sources"]:
        return None
    folder = project_path(workspace.root.resolve(), project_dir)
    if folder is not None:
        for bundle in (folder / start["derived"]).glob("Build/Products/*-iphonesimulator/*.app"):
            if _app_binary(bundle):
                return {"sources": start["sources"], "bundle": bundle.relative_to(folder).as_posix(),
                        "tested": (action == "TEST" and bool(re.search(
                            r"Executed [1-9][0-9]* tests?[, ]|Test run with [1-9][0-9]* tests?", stdout)))}
    return None


def valid_ios_evidence(workspace, project_dir, evidence):
    if not isinstance(evidence, dict) or not evidence.get("sources"):
        return False
    folder = project_path(workspace.root.resolve(), project_dir)
    bundle = evidence.get("bundle", "")
    if not isinstance(bundle, str) or not re.fullmatch(
            r"\.proto-build(?:-[A-Za-z0-9_-]{1,40})?/Build/Products/[^/]+-iphonesimulator/[^/]+\.app", bundle):
        return False
    return (folder is not None and _app_binary(folder / bundle) is not None
            and snapshot(workspace, project_dir, sources=True) == evidence["sources"])
