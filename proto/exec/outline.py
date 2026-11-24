# -*- coding: utf-8 -*-
"""工位接口轮廓：源码里「有哪些类型、各自暴露什么成员」的紧凑投影。

跨文件引用靠想象就会漂移（2026-11-24 生产实证：四个文件四套互不兼容的
SM-2 接口）。轮廓让每一步的固定区都带着工位当前的真实接口，按文件状态
指纹缓存，源码没变就不重算。
"""
from __future__ import annotations

import ast
import os
import re
import stat
from pathlib import Path
from typing import List, Tuple

BUDGET = 2400
MAX_FILES = 40
MAX_TYPES = 24
MAX_MEMBERS = 20
FILE_BYTES = 256 * 1024
SWIFT = {".swift"}
PYTHON = {".py", ".pyi"}
JS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
SOURCE = SWIFT | PYTHON | JS
SKIP_DIRS = {"node_modules", "vendor", "build", "dist", "Pods", "DerivedData", "__pycache__",
             "venv", "coverage", "_archived", "target"}
TRUNCATED = "…（轮廓已截断；用 grep/read_file 查细节）\n"

Entry = Tuple[str, List[str]]

_STRINGS = re.compile(r'"(?:\\.|[^"\\])*"')
_COMMENTS = re.compile(r"//.*$")
_ATTRIBUTES = re.compile(r"^\s*(?:@\w+(?:\([^()\n]*\))?\s+)*")
_MODIFIER = re.compile(r"^\s*(?:public|private|fileprivate|internal|open|override|weak|lazy|"
                       r"nonisolated|optional|final|indirect)(?:\([^)]*\))?\s+")
_SWIFT_TYPE = re.compile(r"^(struct|class|enum|protocol|actor|extension)\s+(\w+)")
_SWIFT_MEMBER = re.compile(r"^(?:(?:class|static)\s+)?(?:mutating\s+)?"
                           r"(?:func\s+\w+|init\b|deinit\b|(?:var|let)\s+\w+)")
_JS_DECL = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)*"
                      r"(?:function\s*\*?\s*(\w+)\s*\(([^)]*)\)|(class|const|let)\s+(\w+))")


def _plain(line: str) -> str:
    """去掉属性行内前缀与访问修饰符，保留 static/class/mutating 这类影响调用的词。"""
    line = _ATTRIBUTES.sub("", line).strip()
    while True:
        match = _MODIFIER.match(line)
        if not match:
            return line.strip()
        line = line[match.end():]


def _signature(text: str) -> str:
    return re.split(r"\s*\{|\s*=(?!=)", text, 1)[0].strip()


def _swift_members(text: str) -> List[str]:
    found = []
    for piece in re.split(r";|\n", text):
        plain = _plain(piece)
        if plain and _SWIFT_MEMBER.match(plain):
            found.append(_signature(plain))
    return found


def swift_outline(text: str) -> List[Entry]:
    """按花括号深度归属成员：类型的直接成员才算，函数体里的局部变量不算。"""
    entries: List[List] = []
    stack: List[Tuple[int, int]] = []      # (body depth, entry index)
    depth = 0
    for raw in text.splitlines():
        code = _COMMENTS.sub("", _STRINGS.sub('""', raw))
        plain = _plain(_COMMENTS.sub("", raw))
        if plain and not plain.startswith("import "):
            if plain.startswith("}"):
                plain = plain[plain.index("}") + 1:].strip()
        stack[:] = [item for item in stack if item[0] <= depth]
        if plain:
            kind = None if re.match(r"^class\s+(?:func|var|let)\b", plain) else _SWIFT_TYPE.match(plain)
            if kind:
                entries.append([f"{kind.group(1)} {kind.group(2)}", [], depth + 1])
                stack.append((depth + 1, len(entries) - 1))
                _, _, rest = plain.partition("{")
                if rest:
                    entries[-1][1].extend(_swift_members(rest.rsplit("}", 1)[0]))
            elif stack and stack[-1][0] == depth and _SWIFT_MEMBER.match(plain):
                entries[stack[-1][1]][1].append(_signature(plain))
        depth = max(0, depth + code.count("{") - code.count("}"))
    return [(header, members) for header, members, _ in entries]


def _unparse(node) -> str:
    try:
        return ast.unparse(node)[:60]
    except (ValueError, TypeError, RecursionError):
        return "?"


def _python_signature(node) -> str:
    args = node.args
    positional = [*args.posonlyargs, *args.args]
    defaults = {len(positional) - len(args.defaults) + index: value
                for index, value in enumerate(args.defaults)}

    def fmt(item, default=None):
        text = item.arg
        if item.annotation is not None:
            text += ": " + _unparse(item.annotation)
        if default is not None:
            text += " = " + _unparse(default)
        return text

    names = [fmt(item, defaults.get(index)) for index, item in enumerate(positional)]
    if args.vararg:
        names.append("*" + fmt(args.vararg))
    elif args.kwonlyargs:
        names.append("*")
    names.extend(fmt(item, default) for item, default in zip(args.kwonlyargs, args.kw_defaults))
    if args.kwarg:
        names.append("**" + fmt(args.kwarg))
    signature = f"{node.name}({', '.join(names)})"
    return signature + (" -> " + _unparse(node.returns) if node.returns is not None else "")


def python_outline(text: str) -> List[Entry]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return []
    entries: List[Entry] = []
    for node in tree.body:
        try:
            entries.extend(_python_entry(node))
        except (ValueError, TypeError, RecursionError, AttributeError):
            continue
    return entries


def _python_entry(node) -> List[Entry]:
    if isinstance(node, ast.ClassDef):
            members = ["class " + item.name if isinstance(item, ast.ClassDef) else _python_signature(item)
                       for item in node.body
                       if isinstance(item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]
            return [("class " + node.name, members)]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return [(_python_signature(node), [])]
    return []


def js_outline(text: str) -> List[Entry]:
    entries: List[Entry] = []
    for raw in text.splitlines():
        match = _JS_DECL.match(raw)
        if not match:
            continue
        if match.group(1):
            entries.append((f"function {match.group(1)}", [f"{match.group(1)}({match.group(2).strip()})"]))
        else:
            entries.append((f"{match.group(3)} {match.group(4)}", [match.group(4)] if match.group(3) != "class" else []))
    return entries


def code_outline(path: Path, text: str) -> List[Entry]:
    suffix = path.suffix.lower()
    if suffix in SWIFT:
        return swift_outline(text)
    if suffix in PYTHON:
        return python_outline(text)
    if suffix in JS:
        return js_outline(text)
    return []


def scan(workspace, project_dir: str = "") -> Tuple[str, List[Path]]:
    """工位源码文件与其状态指纹；指纹不变就不必重算轮廓。"""
    root = Path(getattr(workspace, "root", workspace)).resolve()
    folder = (root / str(project_dir)).resolve() if project_dir else root
    if not folder.is_dir() or not folder.is_relative_to(root):
        return "", []
    files, marks = [], []
    for current, dirs, names in os.walk(folder):
        dirs[:] = sorted(name for name in dirs if name not in SKIP_DIRS and not name.startswith("."))
        for name in sorted(names):
            path = Path(current) / name
            if path.suffix.lower() not in SOURCE or name.startswith("."):
                continue
            try:
                info = path.stat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_size > FILE_BYTES:
                continue
            files.append(path)
            marks.append(f"{path.relative_to(root).as_posix()}:{info.st_mtime_ns}:{info.st_size}")
            if len(files) >= MAX_FILES:
                dirs[:] = []
                break
    return ";".join(marks), sorted(files, key=lambda item: item.as_posix())


def source_outline(workspace, project_dir: str = "", *, budget: int = BUDGET,
                   files: List[Path] = None) -> str:
    if files is None:
        _, files = scan(workspace, project_dir)
    folder = (Path(getattr(workspace, "root", workspace)) / str(project_dir)).resolve() if project_dir \
        else Path(getattr(workspace, "root", workspace)).resolve()
    lines: List[str] = []
    cut = len(files) >= MAX_FILES
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found = code_outline(path, text)
        cut = cut or len(found) > MAX_TYPES
        entries = found[:MAX_TYPES]
        if not entries:
            continue
        lines.append(path.relative_to(folder).as_posix())
        for header, members in entries:
            cut = cut or len(members) > MAX_MEMBERS
            lines.append("  " + header)
            lines.extend("    " + member for member in members[:MAX_MEMBERS])
    if not lines and not cut:
        return ""
    body, used = [], 0
    reserve = len(TRUNCATED.encode("utf-8"))
    for line in lines:
        width = len(line.encode("utf-8", errors="replace")) + 1
        if used + width > budget - reserve:
            return "\n".join(body) + "\n" + TRUNCATED
        body.append(line)
        used += width
    return "\n".join(body) + "\n" + (TRUNCATED if cut else "")
