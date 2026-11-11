# -*- coding: utf-8 -*-
"""M16.1: 终端 UI 层（零依赖）。

零依赖约束下"专业工具"的正确形态：ANSI 色彩 + 制表符 + 步骤指示 + 状态符。
自动降级：非 TTY / NO_COLOR / TERM=dumb 时输出纯文本（CI 与管道安全）。
"""
from __future__ import annotations

import os
import re
import sys
import unicodedata

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_BLUE = "\033[34m"

_ANSI = re.compile(r"\033\[[0-9;]*m")
_forced: bool = None


def force_color(enabled: bool) -> None:
    global _forced
    _forced = enabled


def color_enabled() -> bool:
    if _forced is not None:
        return _forced
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"{code}{text}{_RESET}" if color_enabled() else str(text)


def bold(text): return _c(_BOLD, text)
def dim(text): return _c(_DIM, text)
def cyan(text): return _c(_CYAN, text)
def green(text): return _c(_GREEN, text)
def yellow(text): return _c(_YELLOW, text)
def red(text): return _c(_RED, text)
def blue(text): return _c(_BLUE, text)


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def display_width(text: str) -> int:
    """终端显示宽度：东亚宽字符（中文等）按 2 列计。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in strip_ansi(text))


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - display_width(text))


def banner(title: str, subtitle: str = "") -> str:
    width = max(46, display_width(title) + 4,
                display_width(subtitle) + 4 if subtitle else 0)
    top = "╔" + "═" * width + "╗"
    bot = "╚" + "═" * width + "╝"
    lines = [top, "║ " + _pad(title, width - 1) + "║"]
    if subtitle:
        lines.append("║ " + _pad(subtitle, width - 1) + "║")
    lines.append(bot)
    return _c(_BOLD + _CYAN, "\n".join(lines))


def step(n: int, total: int, text: str) -> str:
    return f"{bold(cyan(f'▸ [{n}/{total}]'))} {text}"


def ok(text: str) -> str:
    return f"{green('✓')} {text}"


def warn(text: str) -> str:
    return f"{yellow('⚠')} {text}"


def fail(text: str) -> str:
    return f"{red('✗')} {text}"


def kv_table(rows, key_width: int = 14) -> str:
    """对齐的键值表（中文键按显示宽度对齐）：[(key, value, value_color_fn_or_None)]"""
    lines = []
    for key, value, *rest in rows:
        color_fn = rest[0] if rest else None
        value_text = color_fn(str(value)) if color_fn else str(value)
        lines.append(f"  {bold(_pad(str(key), key_width))} {value_text}")
    return "\n".join(lines)
