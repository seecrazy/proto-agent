# -*- coding: utf-8 -*-
"""M11: 防休眠（macOS caffeinate，免 sudo）。

安全语义（设计 §3.3 的延伸）：阻止宿主机睡眠是系统级副作用
（耗电/发热/合盖策略），只能由主人显式开启——启动参数 --caffeinate
或控制台「防休眠」开关；agent 永远不能自主调用。

实现：caffeinate -i -w <本进程pid>——断言生命周期绑定到服务进程，
进程退出自动释放，无需清理。诚实边界：caffeinate 不能阻止合盖睡眠
（外接电源+显示器除外）；笔记本用电池时请自行权衡。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Optional


def _default_binary() -> Optional[str]:
    if sys.platform != "darwin":
        return None
    return shutil.which("caffeinate")


class KeepAwake:
    def __init__(self, binary_provider=_default_binary):
        self._binary_provider = binary_provider
        self._proc: Optional[subprocess.Popen] = None

    @property
    def available(self) -> bool:
        return self._binary_provider() is not None

    @property
    def is_active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> bool:
        binary = self._binary_provider()
        if binary is None or self.is_active:
            return self.is_active
        self._proc = subprocess.Popen(
            [binary, "-i", "-w", str(os.getpid())],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
