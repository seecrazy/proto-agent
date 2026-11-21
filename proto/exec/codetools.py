# -*- coding: utf-8 -*-
"""M16: 编码工具族——让 PROTO 自己就是能干的编码 agent。

工作区（workspace_root）是新的信任边界：边界内读写自动放行（审计照记），
run_python 走独立子进程（清洁环境/CPU 限制/超时，本地形态；生产 gVisor）。
路径安全：相对路径、拒绝 `..`/绝对路径，全部圈死在工作区内。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List

from proto.exec.contracts import builtin_parameters

MAX_READ = 50000
MAX_EDIT = 1_000_000
MAX_GREP_HITS = 50


def _safe_rel(rel_path: str) -> Path:
    rel_path = (rel_path or "").strip() or "."
    if rel_path == ".":
        return Path()                                  # 工作区根（LLM 常用 "."）
    parts = Path(rel_path).parts
    if ("\x00" in rel_path or not parts
            or any(p in ("", "..") for p in parts)
            or Path(rel_path).is_absolute()):
        raise ValueError(f"非法工作区相对路径: {rel_path!r}")
    return Path(*parts)


def _limit_resources():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    except Exception:
        pass


def json_quote(value: str) -> str:
    """sandbox-exec profile 字符串参数的安全引号。"""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@lru_cache(maxsize=1)
def _darwin_build_dirs() -> tuple[str, ...]:
    if sys.platform != "darwin":
        return ()
    root = Path("/var/folders").resolve()
    paths = set()
    for name in ("DARWIN_USER_TEMP_DIR", "DARWIN_USER_CACHE_DIR"):
        try:
            found = subprocess.run(["/usr/bin/getconf", name], check=True,
                                   capture_output=True, text=True, timeout=2, env={})
            candidate = Path(found.stdout.strip())
            if candidate.is_absolute():
                path = candidate.resolve(strict=True)
                if root in path.parents and path.is_dir() and path.stat().st_uid == os.geteuid():
                    paths.add(str(path))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
            pass
    return tuple(sorted(paths))


class CodeTools:
    # 无 shell、数组 argv、只开放本机验证工具链。写/网由平台沙盒约束。
    COMMAND_ALLOWLIST = {
        "python", "python3", "pytest", "swift", "swiftc",
        "xcrun", "xcodebuild", "node", "npm", "npx", "yarn", "pnpm",
    }
    XCRUN_TOOLS = {"swift", "swiftc", "xcodebuild", "simctl", "actool"}

    def __init__(self, workspace_root: Path,
                 protected_paths: Iterable[Path] = ()):
        self.root = Path(workspace_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.protected_paths = sorted({
            str(Path(p).expanduser().resolve(strict=False))
            for p in protected_paths if str(p).strip()
        })

    def _abs(self, rel_path: str) -> Path:
        target = self.root / _safe_rel(rel_path)
        root_real = self.root.resolve()
        try:
            target_real = target.resolve(strict=False)
        except OSError as exc:
            raise ValueError(f"无法解析工作区路径: {rel_path!r}") from exc
        if os.path.commonpath([str(root_real), str(target_real)]) != str(root_real):
            raise ValueError(f"路径逃逸工作区: {rel_path!r}")
        return target

    # ---- 文件族 ----
    def write_file(self, path: str, content: str) -> str:
        target = self._abs(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # M59 假勤奋识别：写入内容与现状完全一致=无产出（unchanged），
        # 接力日志据此计入零产出升级，不冒充干活。
        if target.exists() and target.read_text(encoding="utf-8",
                                                errors="replace") == content:
            return f"unchanged:{path}（内容未变——这不是产出，改点真东西）"
        target.write_text(content, encoding="utf-8")
        return f"written:{path}"

    def read_file(self, path: str, offset: int = 0, limit: int = 200):
        """读文件（M48 行级分页，对齐编程 agent）。

        返回带行号文本 + 尾注（总行数/下一页 offset）——大文件不再
        "读不全还不自知"。offset/limit 均按行。
        """
        target = self._abs(path)
        if not target.exists():
            return None
        try:
            offset = max(0, int(offset))
            limit = max(1, min(500, int(limit)))
        except (TypeError, ValueError, OverflowError):
            return "参数错误：offset/limit 需为整数"
        chunk = []
        total, used = 0, 0
        truncated = False
        with target.open(encoding="utf-8", errors="replace") as source:
            for total, line in enumerate(source, 1):
                if offset < total <= offset + limit and used < MAX_READ:
                    text = line.rstrip("\r\n")
                    available = max(0, MAX_READ - used - len(str(total)) - 1)
                    chunk.append((total, text[:available]))
                    used += len(str(total)) + 1 + len(text[:available])
                    truncated = truncated or len(text) > available
        if total == 0:
            return "（空文件）"
        if not chunk:
            return f"offset={offset} 超出范围：文件共 {total} 行（0~{total-1}）"
        body = "\n".join(f"{number}\t{line}" for number, line in chunk)
        last_line = chunk[-1][0]
        footer = f"\n〔共 {total} 行，已读第 {offset+1}-{last_line} 行〕"
        if truncated:
            footer += "；超长行已按本页字符上限截断"
        if last_line < total:
            footer += f"；继续读：read_file(path, offset={last_line})"
        return body + footer

    def read_files(self, paths=None):
        """批量读（M48）：一次调用读多个文件各前 60 行——扫目录/核对
        一组源码不再一个文件烧一步。上限 5 个，总字符封顶防爆上下文。"""
        paths = paths or []
        if isinstance(paths, str):
            paths = [paths]
        paths = paths[:5]
        if not paths:
            return "参数错误：paths 需为文件路径数组（≤5 个）"
        parts, budget = [], 6000
        for p in paths:
            out = str(self.read_file(p, limit=60))
            if out.startswith("参数错误") or "超出范围" in out:
                parts.append(f"### {p}\n{out}")
                continue
            if len(out) > 2000:
                out = out[:2000] + "\n…（单文件截断，read_file 分页读全文）"
            parts.append(f"### {p}\n{out}")
            budget -= len(out)
            if budget <= 0:
                parts.append("（批量预算用尽，其余文件单独 read_file）")
                break
        return "\n\n".join(parts)

    @staticmethod
    def _clip_output(text: str, head: int = 1500, tail: int = 2000) -> str:
        """长输出头尾保留（M48）：测试/编译输出关键信息在头（命令）和
        尾（报错栈），全砍头会丢失败原因。"""
        if not text or len(text) <= head + tail + 100:
            return text
        skipped = len(text) - head - tail
        lines = text.count("\n")
        return (text[:head]
                + f"\n…（中间省略约 {skipped} 字符/{lines} 行）\n"
                + text[-tail:])

    def _read_full(self, path: str) -> str:
        target = self._abs(path)
        if not target.exists():
            raise FileNotFoundError(path)
        if target.stat().st_size > MAX_EDIT:
            raise ValueError(f"文件超过可编辑上限 {MAX_EDIT} 字节: {path}")
        return target.read_text(encoding="utf-8", errors="replace")

    def edit_file(self, path: str, old: str, new: str, replace_all: bool = False) -> int:
        text = self._read_full(path)
        if old not in text:
            raise ValueError(f"未找到要替换的文本片段")
        count = text.count(old)
        if count > 1 and not replace_all:
            raise ValueError(f"片段出现 {count} 次，需 replace_all=True 或更长的唯一片段")
        self.write_file(path, text.replace(old, new) if replace_all else text.replace(old, new, 1))
        return 1 if not replace_all else count

    def list_dir(self, path: str = "") -> str:
        """列工作区目录（目录 2 层 + 项目内文件 1 层）。降本（M45）：
        跳过归档/内部目录、条目上限 40，超出提示用 grep 收窄——
        大目录全量列出只会吃上下文；文件直达免去二次 list_dir。"""
        base = self._abs(path) if path else self.root
        skip_dirs = {"_archived", ".work", "__pycache__"}
        entries = []
        truncated = False
        for current, dirs, files in os.walk(base):
            rel = Path(current).relative_to(self.root)
            depth = len(rel.parts)
            if depth >= 2:
                dirs[:] = []          # 项目目录内不再下钻，但文件要列出
            else:
                dirs[:] = sorted(d for d in dirs if d not in skip_dirs
                                 and not d.startswith("."))
            for name in dirs:
                entries.append(str(rel / name) + "/")
            for name in sorted(files):
                if name.startswith("."):
                    continue
                entries.append(str(rel / name))
            if len(entries) >= 40:
                truncated = True
                break
        listing = "\n".join(entries[:40])
        if truncated:
            listing += "\n…（条目过多已截断；用 grep 按文件名/内容精确收窄）"
        return listing

    def grep(self, pattern: str, glob: str = "*") -> List[Dict]:
        rx = re.compile(pattern)
        hits = []
        for file in sorted(self.root.rglob(glob)):
            try:
                file = self._abs(str(file.relative_to(self.root)))
                if not file.is_file():
                    continue
                for lineno, line in enumerate(
                        file.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if rx.search(line):
                        hits.append({"path": str(file.relative_to(self.root)),
                                     "line": lineno, "text": line.strip()[:200]})
                        if len(hits) >= MAX_GREP_HITS:
                            return hits
            except (OSError, ValueError):
                continue
        return hits

    # ---- Python 执行（默认失败关闭） ----
    @staticmethod
    def execution_backend() -> str:
        configured = os.environ.get("PROTO_EXEC_BACKEND", "auto").strip().lower()
        if configured in ("none", "subprocess", "runsc", "sandbox-exec"):
            return configured
        if sys.platform.startswith("linux") and shutil.which("runsc"):
            return "runsc"
        if sys.platform == "darwin" and shutil.which("sandbox-exec"):
            return "sandbox-exec"
        return "none"

    def _sandbox_exec_profile(self, tmp: str) -> str:
        """写遏制 + 禁网：deny-default 会挡掉 dyld 等系统读导致 SIGABRT，
        这里只锁"能写哪里"（tmp + 工作区）和"不能联网"，读不限制。
        Xcode 等编译链的缓存和系统原子写入使用 Darwin 用户级 TEMP/CACHE，
        不受 TMPDIR 控制；Python 不一定公开相应的 confstr 常量。"""
        allow_write = (os.path.realpath(tmp), str(self.root.resolve()), *_darwin_build_dirs())
        write_rules = "\n".join(
            f"(allow file-write* (subpath {json_quote(p)}))" for p in allow_write)
        protected_denials = "\n".join(
            f"(deny file-{operation}* (subpath {json_quote(p)}))"
            for p in self.protected_paths for operation in ("read", "write"))
        return (
            "(version 1)\n"
            "(allow default)\n"
            "(deny file-write*)\n"
            f"{write_rules}\n"
            f"{protected_denials}\n"
            "(deny network*)\n"
        )

    def run_python(self, code: str, timeout_s: int = 60) -> Dict:
        try:
            timeout_s = max(1, min(120, int(timeout_s)))
        except (TypeError, ValueError):
            return {"exit": -1, "stdout": "", "timeout": False,
                    "stderr": "invalid_timeout"}
        backend = self.execution_backend()
        if backend == "none":
            return {"exit": -1, "stdout": "", "timeout": False,
                    "stderr": ("execution_disabled: 无隔离执行后端；"
                               "设置 PROTO_EXEC_BACKEND=runsc|sandbox-exec，"
                               "或显式设置 subprocess 接受未隔离模式")}
        with tempfile.TemporaryDirectory(prefix="proto-run-") as tmp:
            script = os.path.join(tmp, "snippet.py")
            with open(script, "w", encoding="utf-8") as f:
                f.write(code)
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                   "HOME": tmp, "TMPDIR": tmp}
            if backend == "runsc":
                cmd = [shutil.which("runsc") or "runsc", "do",
                       sys.executable, script]
            elif backend == "sandbox-exec":
                cmd = [shutil.which("sandbox-exec") or "sandbox-exec",
                       "-p", self._sandbox_exec_profile(tmp),
                       sys.executable, script]
            else:
                cmd = [sys.executable, script]
            try:
                proc = subprocess.run(
                    cmd, cwd=self.root, timeout=timeout_s,
                    capture_output=True,
                    env=env,
                    preexec_fn=_limit_resources)
                return {"exit": proc.returncode,
                        "stdout": self._clip_output(
                            proc.stdout.decode("utf-8", "replace")),
                        "stderr": self._clip_output(
                            proc.stderr.decode("utf-8", "replace"),
                            head=800, tail=1500),
                        "timeout": False, "backend": backend}
            except subprocess.TimeoutExpired:
                return {"exit": -1, "stdout": "", "stderr": "timeout",
                        "timeout": True, "backend": backend}

    def run_command(self, argv: List[str], cwd: str = "",
                    timeout_s: int = 120) -> Dict:
        """在项目工作区运行白名单验证命令（无 shell）。"""
        if not isinstance(argv, list) or not (1 <= len(argv) <= 32):
            return {"exit": -1, "stdout": "", "stderr": "invalid_argv",
                    "timeout": False}
        if any(not isinstance(a, str) or "\x00" in a for a in argv):
            return {"exit": -1, "stdout": "", "stderr": "invalid_argv",
                    "timeout": False}
        if sum(len(a) for a in argv) > 8000:
            return {"exit": -1, "stdout": "", "stderr": "argv_too_long",
                    "timeout": False}
        name = Path(argv[0]).name
        if name not in self.COMMAND_ALLOWLIST:
            return {"exit": -1, "stdout": "", "stderr":
                    f"command_not_allowed:{name}", "timeout": False}
        if name == "xcrun" and (len(argv) < 2
                                or Path(argv[1]).name not in self.XCRUN_TOOLS):
            return {"exit": -1, "stdout": "", "stderr": "xcrun_tool_not_allowed",
                    "timeout": False}
        try:
            timeout_s = max(1, min(300, int(timeout_s)))
        except (TypeError, ValueError):
            return {"exit": -1, "stdout": "", "stderr": "invalid_timeout",
                    "timeout": False}
        backend = self.execution_backend()
        if backend == "none":
            return {"exit": -1, "stdout": "", "stderr":
                    ("execution_disabled: 无隔离执行后端；设置 "
                     "PROTO_EXEC_BACKEND=runsc|sandbox-exec"),
                    "timeout": False}
        executable = shutil.which(argv[0])
        if executable is None:
            return {"exit": -1, "stdout": "", "stderr":
                    f"executable_not_found:{argv[0]}", "timeout": False}
        with tempfile.TemporaryDirectory(prefix="proto-cmd-") as tmp:
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                   "HOME": tmp, "TMPDIR": tmp,
                   "PROTO_PROJECT_ROOT": str(self.root.resolve())}
            inner = [executable] + argv[1:]
            if backend == "runsc":
                cmd = [shutil.which("runsc") or "runsc", "do"] + inner
            elif backend == "sandbox-exec":
                cmd = [shutil.which("sandbox-exec") or "sandbox-exec",
                       "-p", self._sandbox_exec_profile(tmp)] + inner
            else:
                cmd = inner
            try:
                proc = subprocess.run(
                    cmd, cwd=self._abs(cwd), timeout=timeout_s,
                    capture_output=True, env=env, preexec_fn=_limit_resources)
                return {"exit": proc.returncode,
                        "stdout": self._clip_output(
                            proc.stdout.decode("utf-8", "replace")),
                        "stderr": self._clip_output(
                            proc.stderr.decode("utf-8", "replace"),
                            head=800, tail=1500),
                        "timeout": False, "backend": backend,
                        "command": [name] + argv[1:]}
            except subprocess.TimeoutExpired:
                return {"exit": -1, "stdout": "", "stderr": "timeout",
                        "timeout": True, "backend": backend}


    # ---- 注册到 Router ----
    def register_all(self, registry) -> None:
        from proto.kernel.kernel import ActionCategory
        def register(name, function, **options):
            registry.register(name, function, parameters=builtin_parameters(name), **options)

        register("read_file",
                          lambda path, offset=0, limit=200:
                          self.read_file(path, offset, limit),
                          category=ActionCategory.READ_DRAFT,
                          description="读工作区文件（行级分页：offset/limit）")
        register("read_files", lambda paths=None: self.read_files(paths),
                          category=ActionCategory.READ_DRAFT,
                          description="批量读多个文件（≤5 个，各前 60 行）")
        register("write_file",
                          lambda path, content: self.write_file(path, content),
                          category=ActionCategory.WORKSPACE_WRITE,
                          description="写工作区文件")
        register("edit_file", lambda path, old, new, replace_all=False:
                          self.edit_file(path, old, new, replace_all),
                          category=ActionCategory.WORKSPACE_WRITE,
                          description="精确替换工作区文件片段")
        register("list_dir", lambda path="": self.list_dir(path),
                          category=ActionCategory.READ_DRAFT,
                          description="列工作区目录")
        register("grep", lambda pattern, glob="*": self.grep(pattern, glob),
                          category=ActionCategory.READ_DRAFT,
                          description="搜索工作区文本")
        # 沙盒后端（写遏制+禁网）下代码只能写 tmp/工作区 → 能力等同 WORKSPACE_WRITE；
        # 未隔离 subprocess 后端必须人工审批；none 后端执行时直接拒绝。
        backend = self.execution_backend()
        run_python_category = (ActionCategory.WORKSPACE_WRITE
                               if backend in ("runsc", "sandbox-exec")
                               else ActionCategory.VISIBLE_WRITE)
        # 未隔离执行=较高权限：主人超时也绝不自主代批
        register("run_python", lambda code, timeout_s=60:
                          self.run_python(code, timeout_s),
                          category=run_python_category,
                          description="沙盒跑 Python（验证/测试）",
                          owner_only=backend not in ("runsc", "sandbox-exec"))
        register(
            "run_command", lambda argv, cwd="", timeout_s=120:
            self.run_command(argv, cwd, timeout_s),
            category=run_python_category,
            description="沙盒跑白名单本机验证命令（数组参数，无 shell）",
            owner_only=backend not in ("runsc", "sandbox-exec"))
