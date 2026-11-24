# -*- coding: utf-8 -*-
"""命令结果的确定性整形：退出码 → 错误摘要 → 输出尾部。

工具返回的原始字典完整留在会话事件与执行日志里；这里只决定模型与时间线看到的
文本把什么放在最前面。压缩与截断都从尾部裁，所以诊断必须在头部。
"""
from __future__ import annotations

import re
from typing import Dict, List

from proto.tools.diagnostics import clean, diagnostics, strip_ansi  # noqa: F401  (re-exported)

HEAD_BYTES = 1500
TAIL_BYTES = 1200
KEY_CHARS = 160
# 成功回执里的结论行：放在最前面，模型和独立验收员都不用翻到输出尾部找证据
RESULT_MARKER = re.compile(
    r"^(?:\*\* (?:BUILD|TEST|TEST BUILD|ARCHIVE|ANALYZE|CLEAN) (?:SUCCEEDED|FAILED) \*\*"
    r"|\*\* TEST EXECUTE (?:SUCCEEDED|FAILED) \*\*"
    r"|Executed \d+ tests?, with .*"
    r"|Test Suite '(?:All tests|[^']*\.xctest)' (?:passed|failed).*"
    r"|\d+ passed.*|.*\d+ failed.*|Build complete! .*)$")


def result_markers(text: str) -> List[str]:
    seen, kept = set(), []
    for line in text.splitlines():
        line = line.strip()
        if RESULT_MARKER.match(line) and line not in seen:
            seen.add(line)
            kept.append(line)
    summary = [line for line in kept if line.startswith("**") or line.startswith("Executed")]
    return (summary + [line for line in kept if line not in summary])[:6]


def dead_end_hints(command, text: str, reason: str, found: List[str]) -> List[str]:
    """几种每次都会撞、且模型自己很难猜出来的死路，直接给出路。"""
    command = [str(item) for item in (command or [])]
    program = command[0] if command else ""
    testing = program in ("xcodebuild", "swift") and "test" in command
    hints = []
    if "Unable to find a device matching the provided destination" in text:
        hints.append("机型或系统版本在本机不存在：用 `xcrun simctl list devices available` 查实际可用的机型，"
                     "照原样填 name（需要时连同 OS）。")
    if (program == "xcodebuild" and "DerivedData" in text and "-derivedDataPath" not in command
            and any(word in text for word in ("Permission", "writable", "Unable to create", "not permitted"))):
        hints.append("沙盒里写不了 Xcode 默认的 DerivedData 目录：加 `-derivedDataPath .proto-build-01`"
                     "（路径必须尚不存在；需要重新取证就换一个后缀）。")
    if any("duplicate symbol '_main'" in item for item in found):
        hints.append("测试入口和模块里的 @main 撞了：带 `@main` 的 App 入口不能和被测代码放在同一个会被"
                     "测试链接的目标里。iOS 应用用生成的 .xcodeproj 跑 `xcodebuild test`（测试 target 由应用托管）；"
                     "要保留 `swift test`，就把 @main 入口拆成单独的 executable target。")
    if reason == "timeout" and testing:
        hints.append("模拟器刚启动时，首次测试可能要 8 分钟以上。先用 `xcrun simctl list devices` 确认该设备"
                     "已是 Booted，再重跑同一条测试命令。")
    return hints


def _tail(text: str, budget: int) -> str:
    data = text.encode("utf-8", errors="replace")
    return data[-budget:].decode("utf-8", errors="ignore").lstrip("\n") if len(data) > budget else text


def _clip(text: str, limit: int) -> str:
    data = text.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return text
    return data[:max(0, limit - 3)].decode("utf-8", errors="ignore") + "…"


def _found(result, combined):
    """优先用工具执行时从完整输出里提取的诊断；旧结果没有这个字段就现场提取。"""
    found = result.get("diagnostics") if isinstance(result, dict) else None
    return [str(item) for item in found] if isinstance(found, list) and found else diagnostics(combined)


def command_view(result, limit: int = 4000, *, base: str = "") -> str:
    if not isinstance(result, dict) or "exit" not in result:
        return str(result)
    exit_code = result.get("exit")
    reason = str(result.get("reason") or ("timeout" if result.get("timeout") else ""))
    stdout = clean(result.get("stdout", ""), base)
    stderr = clean(result.get("stderr", ""), base)
    head = f"退出码 {exit_code}" + (f"（{reason}）" if reason else "") + "\n"
    combined = stdout + ("\n" if stdout and stderr else "") + stderr
    parts = []
    if isinstance(exit_code, int) and exit_code == 0:
        markers = result_markers(combined)
        if markers:
            parts.append("─ 结果 ─\n" + "\n".join(markers))
        if combined.strip():
            parts.append("─ 输出 ─\n" + (_tail(combined, HEAD_BYTES + TAIL_BYTES)
                                       if len(combined.encode("utf-8")) <= HEAD_BYTES + TAIL_BYTES and not markers
                                       else combined[:HEAD_BYTES] + "\n…（中间省略）\n" + _tail(combined, TAIL_BYTES)))
    else:
        found = _found(result, combined)
        if found:
            parts.append(f"─ 错误摘要 {len(found)} 条 ─\n" + "\n".join(found))
        if combined.strip():
            # 有摘要时尾部只留补充信息（链接器报错、说明行），不重复诊断正文
            parts.append("─ 输出尾部 ─\n" + _tail(combined, TAIL_BYTES // 2 if found else TAIL_BYTES))
        hints = dead_end_hints(result.get("command") or [], combined, reason, found)
        if hints:
            parts.append("─ 怎么改 ─\n" + "\n".join(hints))
    if isinstance(result.get("hint"), str) and result["hint"]:
        parts.append("─ 怎么改 ─\n" + result["hint"])
    body = "\n".join(parts)
    return _clip(head + body, limit) if body else head.rstrip("\n")


def failure_key(result, *, base: str = "") -> str:
    """同一验证是否以同样方式失败：看诊断，不看构建横幅与进度行。"""
    if not isinstance(result, dict):
        return ""
    stdout, stderr = str(result.get("stdout", "")), str(result.get("stderr", ""))
    found = _found(result, stdout + "\n" + stderr)
    if found:
        return " ".join("\n".join(found).split())[:KEY_CHARS]
    # 没有可识别的诊断时取报错流的尾部：失败原因几乎总在末尾，开头是横幅与进度
    return " ".join(((stderr or stdout) + " " + str(result.get("reason", ""))).split())[-KEY_CHARS:]
