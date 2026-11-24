# -*- coding: utf-8 -*-
"""从命令输出里取出最能指导下一步的行（纯函数，工具层与执行循环共用）。"""
from __future__ import annotations

import os
import re
from typing import List

DIAGNOSTIC_LIMIT = 12
DIAGNOSTIC_BYTES = 2400
EXCERPT_LINES = 4
SUMMARY_LINES = 6

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
# clang / swiftc：路径:行[:列]: error: 消息
LOCATED = re.compile(r"^[^\s:][^:\n]*?:\d+(?::\d+)?: (?:fatal )?error: ?")
EXCERPT = re.compile(r"^\s*(?:\d+ \||\|)")
PY_FILE = re.compile(r'^\s*File "[^"]+", line \d+(?:, in \S+)?')
PYTEST_SUMMARY = re.compile(r"^(?:FAILED|ERROR) \S.*$")
PYTEST_DETAIL = re.compile(r"^E {3,}\S.*$")
FAILED_COMMANDS = re.compile(r"^The following build commands failed:$")
INDENTED = re.compile(r"^\s+\S.*$")
GENERIC = re.compile(
    r"^(?:"
    r"\*{2} (?:BUILD|TEST|ARCHIVE|ANALYZE) FAILED \*\*"
    r"|(?:xcodebuild|swift|swiftc|npm|npx|node|python3?|pip3?|git): (?:fatal )?error:.*"
    r"|error:.*"
    r"|fatal:.*"
    r"|error TS\d+:.*"
    r"|npm (?:ERR!|error|ERR) (?!errno\b|syscall\b|path\b).*"
    r"|Uncaught \w*Error.*"
    r"|(?:[A-Za-z_]\w*\.)*[A-Z]\w*Error: .*"
    r"|E {3,}.*"
    r"|.*(?:Operation not permitted|Permission denied|No such file or directory|command not found)"
    r")$")


def strip_ansi(text) -> str:
    return ANSI.sub("", str(text))


def clean(text, base: str = "") -> str:
    text = strip_ansi(text).replace("\r\n", "\n")
    for prefix in (base, os.path.realpath(base) if base else ""):
        # 只裁绝对路径前缀：相对前缀会匹配到输出里绝对路径的中段，留下被截断的假路径
        if prefix and os.path.isabs(prefix):
            text = text.replace(prefix.rstrip("/") + "/", "")
    return text


def _dedupe(items):
    seen, kept = set(), []
    for item in items:
        key = " ".join(item.split())
        if key and key not in seen:
            seen.add(key)
            kept.append(item)
    return kept


def _bound(items: List[str], limit: int, budget: int) -> List[str]:
    kept, used = [], 0
    for item in items:
        size = len(item.encode("utf-8", errors="replace"))
        if kept and used + size > budget:
            break
        if size > budget:
            item = item.encode("utf-8", errors="replace")[:budget - 1].decode("utf-8", errors="ignore") + "…"
            size = budget
        kept.append(item)
        used += size + 1
        if len(kept) >= limit:
            break
    return kept


def diagnostics(text, *, base: str = "", limit: int = DIAGNOSTIC_LIMIT,
                budget: int = DIAGNOSTIC_BYTES) -> List[str]:
    """带位置的编译错误优先；没有时退回通用错误行（Python 栈、pytest 汇总、构建清单）。"""
    lines = clean(text, base).splitlines()
    summary = [line for line in lines if PYTEST_SUMMARY.match(line)]
    if summary:
        return _bound(_dedupe(summary), limit, budget)
    located, index = [], 0
    while index < len(lines):
        if LOCATED.match(lines[index]):
            block = [lines[index]]
            for follow in lines[index + 1:index + 1 + EXCERPT_LINES]:
                if not EXCERPT.match(follow):
                    break
                block.append(follow)
            located.append("\n".join(block))
        index += 1
    if located:
        return _bound(_dedupe(located), limit, budget)
    selected, index = [], 0
    while index < len(lines):
        line = lines[index]
        if FAILED_COMMANDS.match(line):
            block = [line]
            for follow in lines[index + 1:index + 1 + SUMMARY_LINES]:
                if not INDENTED.match(follow):
                    break
                block.append(follow)
            selected.append("\n".join(block))
            index += len(block)
            continue
        if PY_FILE.match(line) or PYTEST_DETAIL.match(line) or GENERIC.match(line):
            selected.append(line)
        index += 1
    return _bound(_dedupe(selected), limit, budget)
