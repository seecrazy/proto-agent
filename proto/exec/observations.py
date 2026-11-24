# -*- coding: utf-8 -*-
"""命令结果的确定性整形：退出码 → 错误摘要 → 输出尾部。

工具返回的原始字典完整留在会话事件与执行日志里；这里只决定模型与时间线看到的
文本把什么放在最前面。压缩与截断都从尾部裁，所以诊断必须在头部。
"""
from __future__ import annotations

import os
import re
from typing import Dict, List

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
DIAGNOSTIC_LIMIT = 12
DIAGNOSTIC_BYTES = 2400
HEAD_BYTES = 1500
TAIL_BYTES = 1200
KEY_CHARS = 160
EXCERPT_LINES = 4
SUMMARY_LINES = 6

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


def strip_ansi(text: str) -> str:
    return ANSI.sub("", str(text))


def _clean(text, base: str) -> str:
    text = strip_ansi(text).replace("\r\n", "\n")
    for prefix in (base, os.path.realpath(base) if base else ""):
        if prefix:
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
    """从命令输出里取出最能指导下一步的行；没有带位置的编译错误时退回通用错误行。"""
    lines = _clean(text, base).splitlines()
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


def _tail(text: str, budget: int) -> str:
    data = text.encode("utf-8", errors="replace")
    return data[-budget:].decode("utf-8", errors="ignore").lstrip("\n") if len(data) > budget else text


def _clip(text: str, limit: int) -> str:
    data = text.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return text
    return data[:max(0, limit - 3)].decode("utf-8", errors="ignore") + "…"


def command_view(result, limit: int = 4000, *, base: str = "") -> str:
    if not isinstance(result, dict) or "exit" not in result:
        return str(result)
    exit_code = result.get("exit")
    reason = str(result.get("reason") or ("timeout" if result.get("timeout") else ""))
    stdout = _clean(result.get("stdout", ""), base)
    stderr = _clean(result.get("stderr", ""), base)
    head = f"退出码 {exit_code}" + (f"（{reason}）" if reason else "") + "\n"
    combined = stdout + ("\n" if stdout and stderr else "") + stderr
    parts = []
    if isinstance(exit_code, int) and exit_code == 0:
        if combined.strip():
            parts.append("─ 输出 ─\n" + (_tail(combined, HEAD_BYTES + TAIL_BYTES)
                                       if len(combined.encode("utf-8")) <= HEAD_BYTES + TAIL_BYTES
                                       else combined[:HEAD_BYTES] + "\n…（中间省略）\n" + _tail(combined, TAIL_BYTES)))
    else:
        found = diagnostics(combined)
        if found:
            parts.append(f"─ 错误摘要 {len(found)} 条 ─\n" + "\n".join(found))
        if combined.strip():
            # 有摘要时尾部只留补充信息（链接器报错、说明行），不重复诊断正文
            parts.append("─ 输出尾部 ─\n" + _tail(combined, TAIL_BYTES // 2 if found else TAIL_BYTES))
    body = "\n".join(parts)
    return _clip(head + body, limit) if body else head.rstrip("\n")


def failure_key(result, *, base: str = "") -> str:
    """同一验证是否以同样方式失败：看诊断，不看构建横幅与进度行。"""
    if not isinstance(result, dict):
        return ""
    stdout, stderr = str(result.get("stdout", "")), str(result.get("stderr", ""))
    found = diagnostics(stdout + "\n" + stderr, base=base)
    if found:
        return " ".join("\n".join(found).split())[:KEY_CHARS]
    # 没有可识别的诊断时取报错流的尾部：失败原因几乎总在末尾，开头是横幅与进度
    return " ".join(((stderr or stdout) + " " + str(result.get("reason", ""))).split())[-KEY_CHARS:]
