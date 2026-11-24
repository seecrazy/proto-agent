# -*- coding: utf-8 -*-
"""命令结果的确定性整形：退出码 → 错误摘要 → 输出尾部。

工具返回的原始字典完整留在会话事件与执行日志里；这里只决定模型与时间线看到的
文本把什么放在最前面。压缩与截断都从尾部裁，所以诊断必须在头部。
"""
from __future__ import annotations

from typing import Dict, List

from proto.tools.diagnostics import clean, diagnostics, strip_ansi  # noqa: F401  (re-exported)

HEAD_BYTES = 1500
TAIL_BYTES = 1200
KEY_CHARS = 160


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
        if combined.strip():
            parts.append("─ 输出 ─\n" + (_tail(combined, HEAD_BYTES + TAIL_BYTES)
                                       if len(combined.encode("utf-8")) <= HEAD_BYTES + TAIL_BYTES
                                       else combined[:HEAD_BYTES] + "\n…（中间省略）\n" + _tail(combined, TAIL_BYTES)))
    else:
        found = _found(result, combined)
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
    found = _found(result, stdout + "\n" + stderr)
    if found:
        return " ".join("\n".join(found).split())[:KEY_CHARS]
    # 没有可识别的诊断时取报错流的尾部：失败原因几乎总在末尾，开头是横幅与进度
    return " ".join(((stderr or stdout) + " " + str(result.get("reason", ""))).split())[-KEY_CHARS:]
