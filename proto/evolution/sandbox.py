# -*- coding: utf-8 -*-
"""阶段 7：变体验证沙盒（设计 §9.1 代码轨）。

可插拔后端（与 proto.exec.codetools 同一词汇表，默认失败关闭）：
  auto（默认）——Linux + runsc → runsc；macOS + sandbox-exec → sandbox-exec；
      都没有则 none（拒绝执行）；
  runsc（gVisor）——生产形态（Linux + 安装 runsc）；
  sandbox-exec——macOS 隔离（写遏制 + 禁网）；
  subprocess——未隔离子进程，仅显式指定（构造参数或
      PROTO_EVOLUTION_BACKEND=subprocess）时启用；
  none——不执行，全部用例判失败（execution_disabled）。
接口不变——run_variant(code, cases)。
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import List

_RUNNER = r"""
import json, importlib.util
spec = importlib.util.spec_from_file_location("variant", "variant.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
results = []
for case in json.load(open("cases.json", encoding="utf-8")):
    try:
        got = mod.solve(case["input"])
        results.append({"ok": got == case["expected"], "error": None})
    except Exception as exc:
        results.append({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
json.dump(results, open("out.json", "w", encoding="utf-8"))
"""


@dataclass
class SandboxResult:
    total: int
    outcomes: List[dict] = field(default_factory=list)
    timeout: bool = False

    @property
    def passed(self) -> int:
        return sum(1 for o in self.outcomes if o["ok"])

    @property
    def errors(self) -> List[str]:
        return [o["error"] for o in self.outcomes if o.get("error")]


def _limit_resources():
    """best-effort CPU 限制（Linux/macOS 可用性不同，失败静默跳过）。"""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    except Exception:
        pass


def _sandbox_exec_profile(tmp: str) -> str:
    """写遏制 + 禁网（同 codetools：deny-default 会挡掉系统读导致进程被杀）。"""

    def q(value: str) -> str:
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'

    real = os.path.realpath(tmp)
    return (
        "(version 1)\n"
        "(allow default)\n"
        "(deny file-write*)\n"
        f"(allow file-write* (subpath {q(real)}))\n"
        "(deny network*)\n"
    )


class Sandbox:
    _BACKENDS = ("auto", "runsc", "sandbox-exec", "subprocess", "none")

    def __init__(self, timeout_s: int = 10, backend: str = "auto",
                 runsc_path: str = None):
        self.timeout_s = timeout_s
        self.runsc_path = runsc_path or shutil.which("runsc")
        backend = backend or os.environ.get("PROTO_EVOLUTION_BACKEND", "auto")
        if backend not in self._BACKENDS:
            raise ValueError(f"未知沙盒后端: {backend}")
        if backend == "auto":
            if self.runsc_path and platform.system() == "Linux":
                self.backend = "runsc"
            elif (platform.system() == "Darwin"
                  and shutil.which("sandbox-exec")):
                self.backend = "sandbox-exec"
            else:
                self.backend = "none"
        else:
            self.backend = backend
            if backend == "runsc" and not self.runsc_path:
                raise ValueError("backend=runsc 需要 runsc（Linux + gVisor 安装）")

    def _command(self) -> List[str]:
        if self.backend == "runsc":
            return [self.runsc_path, "do", sys.executable, "runner.py"]
        if self.backend == "sandbox-exec":
            return ["sandbox-exec", "-p", self._profile, sys.executable,
                    "runner.py"]
        return [sys.executable, "runner.py"]

    def run_variant(self, code: str, cases: List[dict]) -> SandboxResult:
        if self.backend == "none":
            return SandboxResult(
                total=len(cases),
                outcomes=[{"ok": False, "error": "execution_disabled"}] * len(cases))
        with tempfile.TemporaryDirectory(prefix="proto-sandbox-") as tmp:
            self._profile = _sandbox_exec_profile(tmp)
            with open(os.path.join(tmp, "variant.py"), "w", encoding="utf-8") as f:
                f.write(code)
            with open(os.path.join(tmp, "runner.py"), "w", encoding="utf-8") as f:
                f.write(_RUNNER)
            with open(os.path.join(tmp, "cases.json"), "w", encoding="utf-8") as f:
                json.dump(cases, f, ensure_ascii=False)
            try:
                proc = subprocess.run(
                    self._command(),
                    cwd=tmp,
                    timeout=self.timeout_s,
                    capture_output=True,
                    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                    preexec_fn=_limit_resources,
                )
            except subprocess.TimeoutExpired:
                return SandboxResult(total=len(cases), timeout=True,
                                     outcomes=[{"ok": False, "error": "timeout"}] * len(cases))
            out_path = os.path.join(tmp, "out.json")
            if proc.returncode != 0 or not os.path.exists(out_path):
                stderr = (proc.stderr or b"").decode("utf-8", "replace")[-300:]
                return SandboxResult(total=len(cases),
                                     outcomes=[{"ok": False, "error": f"crash: {stderr}"}] * len(cases))
            with open(out_path, encoding="utf-8") as f:
                outcomes = json.load(f)
            return SandboxResult(total=len(cases), outcomes=outcomes)
