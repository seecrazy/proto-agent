# -*- coding: utf-8 -*-
"""阶段 7：变体验证沙盒（设计 §9.1 代码轨）。

本地形态：独立 subprocess + 临时工作目录 + 清洁环境 + CPU 时间/超时约束 +
用例串行执行。生产目标形态：gVisor / microVM 级隔离（设计 §4.4），
接口不变——run_variant(code, cases) → SandboxResult。
"""
from __future__ import annotations

import json
import os
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


class Sandbox:
    def __init__(self, timeout_s: int = 10):
        self.timeout_s = timeout_s

    def run_variant(self, code: str, cases: List[dict]) -> SandboxResult:
        with tempfile.TemporaryDirectory(prefix="proto-sandbox-") as tmp:
            with open(os.path.join(tmp, "variant.py"), "w", encoding="utf-8") as f:
                f.write(code)
            with open(os.path.join(tmp, "runner.py"), "w", encoding="utf-8") as f:
                f.write(_RUNNER)
            with open(os.path.join(tmp, "cases.json"), "w", encoding="utf-8") as f:
                json.dump(cases, f, ensure_ascii=False)
            try:
                proc = subprocess.run(
                    [sys.executable, "runner.py"],
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
