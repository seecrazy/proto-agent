"""Short, shared admission for model traffic, independent of daily budgets."""
from __future__ import annotations

import math
import threading
import time
from collections import Counter
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError

from proto.llm.client import ModelPressureWait


# 指数退避的封顶时长。冷却截止时间比它还远的只可能是「额度用尽、等重置」——
# 冷却表不存原因，这个不变量让重启之后仍然说得出自己是哪种冷却。
_MAX_BACKOFF_S = 1800


def cooldown_reason(retry_at, now):
    return "model_quota_exhausted" if retry_at - now > _MAX_BACKOFF_S else "model_rate_limited"


def retry_after(error, now):
    while error is not None and not isinstance(error, HTTPError):
        error = error.__cause__
    value = error.headers.get("Retry-After", "") if error and error.headers else ""
    value = value.strip()
    if not value or len(value) > 80:
        return None
    try:
        if value.isascii() and value.isdecimal():
            return now + int(value)
        deadline = parsedate_to_datetime(value)
        return deadline.timestamp() if deadline.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


class ModelPressure:
    def __init__(self, database, *, now=time.time, foreground_wait=2.0):
        self.database = database
        self.now = now
        self.foreground_wait = foreground_wait
        self._condition = threading.Condition()
        self._inflight = set()
        self._front = Counter()

    def _retry_at(self, route, origin):
        with self.database.connect() as connection:
            return connection.execute("SELECT coalesce(max(retry_at), 0) FROM model_pressure WHERE route IN (?, ?)",
                                      (route, origin)).fetchone()[0]

    def snapshot(self, route, *, origin=None):
        if route:
            keys = (route, origin) if origin else (route,)
            with self._condition:
                retry_at = self._retry_at(route, origin)
                if retry_at > self.now():
                    return {"state": "cooldown", "waiting": True,
                            "reason": cooldown_reason(retry_at, self.now()), "retry_at": retry_at}
                if any(key in self._inflight or self._front.get(key) for key in keys):
                    return {"state": "busy", "waiting": True, "reason": "model_route_busy", "retry_at": None}
        return None

    @contextmanager
    def admit(self, route, *, origin=None, foreground=False):
        if not route:
            yield
            return
        keys = (route, origin) if origin else (route,)
        with self._condition:
            if foreground:
                self._front.update(keys)
            try:
                wait_until = time.monotonic() + self.foreground_wait
                while True:
                    retry_at = self._retry_at(route, origin)
                    if retry_at > self.now():
                        raise ModelPressureWait(cooldown_reason(retry_at, self.now()), retry_at)
                    if not any(key in self._inflight or (not foreground and self._front.get(key)) for key in keys):
                        self._inflight.update(keys)
                        break
                    remaining = wait_until - time.monotonic()
                    if not foreground or remaining <= 0:
                        raise ModelPressureWait("model_route_busy")
                    self._condition.wait(remaining)
            finally:
                if foreground:
                    for key in keys:
                        self._front[key] -= 1
                        if not self._front[key]:
                            del self._front[key]
        try:
            yield
        finally:
            with self._condition:
                self._inflight.difference_update(keys)
                self._condition.notify_all()

    def limited(self, route, error, *, origin=None):
        now = self.now()
        hint = retry_after(error, now)
        deadlines = {}
        with self.database.connect(write=True) as connection:
            for key in (route, origin) if origin else (route,):
                row = connection.execute("SELECT strikes, retry_at FROM model_pressure WHERE route = ?",
                                         (key,)).fetchone()
                strikes = min((row["strikes"] if row else 0) + 1, 6)
                delay = min(_MAX_BACKOFF_S, 60 * 2 ** (strikes - 1))
                retry_at = max(now + 1, hint) if hint is not None and math.isfinite(hint) else now + delay
                if row:
                    retry_at = max(retry_at, row["retry_at"])
                connection.execute("INSERT OR REPLACE INTO model_pressure VALUES (?, ?, ?)",
                                   (key, retry_at, strikes))
                deadlines[key] = retry_at
        return ModelPressureWait("model_rate_limited", deadlines[route])

    def quarantine(self, route, retry_at, *, origin=None):
        """接入额度用尽：只隔离这一个接入，直到供应方说的重置时刻。

        不写同源那一行——额度是按接入（密钥）计的，同主机上别的接入、别的模型照常工作。
        限流（limited）才会连带同源：那是账号级的「请求太密」。"""
        if not route:
            return ModelPressureWait("model_quota_exhausted", retry_at)
        with self.database.connect(write=True) as connection:
            row = connection.execute("SELECT retry_at FROM model_pressure WHERE route = ?", (route,)).fetchone()
            deadline = max(float(retry_at or 0), row["retry_at"] if row else 0)
            connection.execute("INSERT OR REPLACE INTO model_pressure VALUES (?, ?, ?)", (route, deadline, 0))
        return ModelPressureWait("model_quota_exhausted", deadline)

    def succeeded(self, route, *, origin=None):
        if not route:
            return
        where = "FROM model_pressure WHERE route IN (?, ?) AND retry_at <= ?"
        args = (route, origin, self.now())
        with self.database.connect() as connection:
            expired = connection.execute(f"SELECT 1 {where} LIMIT 1", args).fetchone()
        if expired is not None:
            with self.database.connect(write=True) as connection:
                connection.execute(f"DELETE {where}", args)
