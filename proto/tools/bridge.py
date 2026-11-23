# -*- coding: utf-8 -*-
"""M16: 外部编码 agent 桥——把 zcode/claude/codex 等 CLI 变成 PROTO 的工具。

哲学：PROTO 出意图、记经验、担审批；专业工具出执行力。
检测到 CLI 才注册；调用属 visible_write 级（需主人批准一次），
超时与输出截断防失控。按 PROTO_BRIDGE_CLI 显式指定，或按
claude/codex/zcode 顺序自动探测。
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Optional

CANDIDATES = ("claude", "codex", "zcode")
MAX_OUTPUT = 20000
TIMEOUT_S = 600


class CodingBridge:
    def __init__(self, cli_path: Optional[str] = None, cli_name: str = "",
                 timeout_s: int = TIMEOUT_S):
        self._path = cli_path
        self._name = cli_name or (cli_path or "").split("/")[-1] or "cli"
        self._timeout = timeout_s

    @classmethod
    def autodetect(cls, explicit: str = "") -> Optional["CodingBridge"]:
        if explicit:
            found = shutil.which(explicit)
            return cls(found, explicit) if found else None
        for name in CANDIDATES:
            found = shutil.which(name)
            if found:
                return cls(found, name)
        return None

    @property
    def available(self) -> bool:
        return bool(self._path)

    def describe(self) -> str:
        return f"{self._name} ({self._path})" if self._path else "（未检测到外部编码 CLI）"

    def delegate(self, task: str) -> str:
        """把任务外包给外部 CLI，返回输出。失败返回错误说明字符串。"""
        if not self._path:
            return "错误：未配置外部编码 agent（设 PROTO_BRIDGE_CLI=claude|codex|zcode）"
        args = [self._path]
        if self._name == "claude":
            args += ["-p", task]
        elif self._name == "codex":
            args += ["exec", task]
        else:
            args += [task]
        try:
            proc = subprocess.run(args, capture_output=True, timeout=self._timeout,
                                  text=True)
            output = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr)
                                            if proc.stderr else "")
            return output.strip()[:MAX_OUTPUT] or f"（无输出，exit={proc.returncode}）"
        except subprocess.TimeoutExpired:
            return f"错误：外部 agent 超时（{self._timeout}s）"
        except OSError as exc:
            return f"错误：无法启动 {self._name}: {exc}"
