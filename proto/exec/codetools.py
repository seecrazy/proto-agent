# -*- coding: utf-8 -*-
"""M16: 编码工具族——让 PROTO 自己就是能干的编码 agent。

工作区（workspace_root）是新的信任边界：边界内读写自动放行（审计照记），
run_python 走独立子进程（清洁环境/CPU 限制/超时，本地形态；生产 gVisor）。
路径安全：相对路径、拒绝 `..`/绝对路径，全部圈死在工作区内。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

MAX_READ = 50000
MAX_GREP_HITS = 50


def _safe_rel(rel_path: str) -> Path:
    parts = Path(rel_path).parts
    if not parts or any(p in ("", ".", "..") for p in parts) or Path(rel_path).is_absolute():
        raise ValueError(f"非法工作区相对路径: {rel_path!r}")
    return Path(*parts)


def _limit_resources():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    except Exception:
        pass


class CodeTools:
    def __init__(self, workspace_root: Path):
        self.root = Path(workspace_root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _abs(self, rel_path: str) -> Path:
        return self.root / _safe_rel(rel_path)

    # ---- 文件族 ----
    def write_file(self, path: str, content: str) -> str:
        target = self._abs(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"written:{path}"

    def read_file(self, path: str):
        target = self._abs(path)
        if not target.exists():
            return None
        return target.read_text(encoding="utf-8", errors="replace")[:MAX_READ]

    def edit_file(self, path: str, old: str, new: str, replace_all: bool = False) -> int:
        text = self.read_file(path)
        if text is None:
            raise FileNotFoundError(path)
        if old not in text:
            raise ValueError(f"未找到要替换的文本片段")
        count = text.count(old)
        if count > 1 and not replace_all:
            raise ValueError(f"片段出现 {count} 次，需 replace_all=True 或更长的唯一片段")
        self.write_file(path, text.replace(old, new) if replace_all else text.replace(old, new, 1))
        return 1 if not replace_all else count

    def list_dir(self, path: str = "") -> str:
        base = self._abs(path) if path else self.root
        entries = []
        for current, dirs, files in os.walk(base):
            depth = len(Path(current).relative_to(self.root).parts)
            if depth >= 2:
                dirs[:] = []
                continue
            for name in sorted(dirs):
                entries.append(str(Path(current).relative_to(self.root) / name) + "/")
            for name in sorted(files):
                entries.append(str(Path(current).relative_to(self.root) / name))
            if len(entries) >= 200:
                break
        return "\n".join(entries[:200])

    def grep(self, pattern: str, glob: str = "*") -> List[Dict]:
        rx = re.compile(pattern)
        hits = []
        for file in sorted(self.root.rglob(glob)):
            if not file.is_file() or file.is_dir():
                continue
            try:
                for lineno, line in enumerate(
                        file.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if rx.search(line):
                        hits.append({"path": str(file.relative_to(self.root)),
                                     "line": lineno, "text": line.strip()[:200]})
                        if len(hits) >= MAX_GREP_HITS:
                            return hits
            except OSError:
                continue
        return hits

    # ---- Python 执行（本地沙盒形态） ----
    def run_python(self, code: str, timeout_s: int = 60) -> Dict:
        with tempfile.TemporaryDirectory(prefix="proto-run-") as tmp:
            script = os.path.join(tmp, "snippet.py")
            with open(script, "w", encoding="utf-8") as f:
                f.write(code)
            try:
                proc = subprocess.run(
                    [sys.executable, script], cwd=tmp, timeout=timeout_s,
                    capture_output=True,
                    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                         "HOME": tmp},
                    preexec_fn=_limit_resources)
                return {"exit": proc.returncode,
                        "stdout": proc.stdout.decode("utf-8", "replace")[:20000],
                        "stderr": proc.stderr.decode("utf-8", "replace")[:4000],
                        "timeout": False}
            except subprocess.TimeoutExpired:
                return {"exit": -1, "stdout": "", "stderr": "timeout",
                        "timeout": True}

    # ---- 注册到 Router ----
    def register_all(self, registry) -> None:
        registry.register("read_file", lambda path: self.read_file(path))
        registry.register("write_file", lambda path, content: self.write_file(path, content))
        registry.register("edit_file", lambda path, old, new, replace_all=False:
                          self.edit_file(path, old, new, replace_all))
        registry.register("list_dir", lambda path="": self.list_dir(path))
        registry.register("grep", lambda pattern, glob="*": self.grep(pattern, glob))
        registry.register("run_python", lambda code, timeout_s=60:
                          self.run_python(code, timeout_s))
