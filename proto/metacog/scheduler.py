# -*- coding: utf-8 -*-
"""M4-③: L3 运行节奏调度器（设计 L3 运行节奏）。

节奏：策略蒸馏每任务完成即时触发；目标生成每 4 小时；深度回顾每日。
时钟可注入（离线可测）。调度器只负责"何时"，回调由外部注册——
后续接 LangGraph 时每个回调即一个图节点。
"""
from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional


class MetacogScheduler:
    def __init__(self, goal_interval_s: float = 4 * 3600,
                 deep_review_interval_s: float = 86400,
                 clock: Callable[[], float] = time.time):
        self._goal_interval = goal_interval_s
        self._deep_interval = deep_review_interval_s
        self._clock = clock
        self._callbacks: Dict[str, Callable] = {}
        self._last_goal: Optional[float] = None
        self._last_deep: Optional[float] = None

    def start(self) -> None:
        now = self._clock()
        self._last_goal = now
        self._last_deep = now

    def register(self, event: str, callback: Callable) -> None:
        self._callbacks[event] = callback

    def tick(self, now: Optional[float] = None) -> List[str]:
        now = self._clock() if now is None else now
        fired: List[str] = []
        if now - self._last_goal >= self._goal_interval:
            self._fire("goal_generation", fired)
            self._last_goal = now
        if now - self._last_deep >= self._deep_interval:
            self._fire("deep_review", fired)
            self._last_deep = now
        return fired

    def on_task_completed(self, task_result) -> None:
        """策略蒸馏：每任务完成即时触发（设计 §7.3 机制三）。"""
        callback = self._callbacks.get("distill")
        if callback is not None:
            callback(task_result)

    # ---- 持久化 ----
    def to_dict(self) -> dict:
        return {"last_goal": self._last_goal, "last_deep": self._last_deep}

    @classmethod
    def from_dict(cls, raw: dict, goal_interval_s: float = 4 * 3600,
                  deep_review_interval_s: float = 86400) -> "MetacogScheduler":
        s = cls(goal_interval_s=goal_interval_s,
                deep_review_interval_s=deep_review_interval_s)
        s._last_goal = raw.get("last_goal", 0.0)
        s._last_deep = raw.get("last_deep", 0.0)
        return s

    def _fire(self, event: str, fired: List[str]) -> None:
        fired.append(event)
        callback = self._callbacks.get(event)
        if callback is not None:
            callback()
