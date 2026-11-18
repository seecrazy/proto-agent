# -*- coding: utf-8 -*-
"""M12: 单实例锁——同一数据目录只允许一个 proto.web 存活。

防止双实例并发写状态/审计（AuditSpine 的 seq 缓存是进程内的，
双写会产生断链）。锁文件带 pid：陈旧锁（进程已死）自动复用。
"""
from __future__ import annotations

import os
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


class InstanceLock:
    def __init__(self, data_dir: Path):
        self._path = Path(data_dir) / "proto.lock"
        self._fd = None

    def acquire(self) -> bool:
        if self._fd is not None:
            return True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if fcntl is not None:
            fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                return False
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode("utf-8"))
            os.fsync(fd)
            self._fd = fd
            return True
        # 无 fcntl 的平台：保守地使用 O_EXCL + 陈旧 PID 检测。
        if self._path.exists():
            try:
                old_pid = int(self._path.read_text(encoding="utf-8").strip())
            except ValueError:
                old_pid = 0
            if old_pid and _pid_alive(old_pid):
                return False   # 严格：持有者存活（含自身路径重入）即拒绝
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        os.close(fd)
        self._fd = "fallback"
        return True

    def release(self) -> None:
        if self._fd is not None and fcntl is not None and self._fd != "fallback":
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            return
        if self._path.exists():
            try:
                if int(self._path.read_text(encoding="utf-8").strip()) == os.getpid():
                    self._path.unlink()
            except (ValueError, OSError):
                pass
        self._fd = None
