# -*- coding: utf-8 -*-
"""M12: 单实例锁——同一数据目录只允许一个 proto.web 存活。

防止双实例并发写状态/审计（AuditSpine 的 seq 缓存是进程内的，
双写会产生断链）。锁文件带 pid：陈旧锁（进程已死）自动复用。
"""
from __future__ import annotations

import os
from pathlib import Path


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


class InstanceLock:
    def __init__(self, data_dir: Path):
        self._path = Path(data_dir) / "proto.lock"

    def acquire(self) -> bool:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            try:
                old_pid = int(self._path.read_text(encoding="utf-8").strip())
            except ValueError:
                old_pid = 0
            if old_pid and _pid_alive(old_pid):
                return False   # 严格：持有者存活（含自身路径重入）即拒绝
        self._path.write_text(str(os.getpid()), encoding="utf-8")
        return True

    def release(self) -> None:
        if self._path.exists():
            try:
                if int(self._path.read_text(encoding="utf-8").strip()) == os.getpid():
                    self._path.unlink()
            except (ValueError, OSError):
                pass
