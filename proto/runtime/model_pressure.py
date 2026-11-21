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

    def _cooldown(self, route):
        with self.database.connect() as connection:
            return connection.execute("SELECT retry_at, strikes FROM model_pressure WHERE route = ?",
                                      (route,)).fetchone()

    def snapshot(self, route):
        if route:
            with self._condition:
                row = self._cooldown(route)
                if row and row["retry_at"] > self.now():
                    return {"state": "cooldown", "waiting": True, "reason": "model_rate_limited",
                            "retry_at": row["retry_at"]}
                if route in self._inflight or self._front.get(route):
                    return {"state": "busy", "waiting": True, "reason": "model_route_busy", "retry_at": None}
        return None

    @contextmanager
    def admit(self, route, *, foreground=False):
        if not route:
            yield
            return
        with self._condition:
            if foreground:
                self._front[route] += 1
            try:
                wait_until = time.monotonic() + self.foreground_wait
                while True:
                    row = self._cooldown(route)
                    if row and row["retry_at"] > self.now():
                        raise ModelPressureWait("model_rate_limited", row["retry_at"])
                    if route not in self._inflight and (foreground or not self._front.get(route)):
                        self._inflight.add(route)
                        break
                    remaining = wait_until - time.monotonic()
                    if not foreground or remaining <= 0:
                        raise ModelPressureWait("model_route_busy")
                    self._condition.wait(remaining)
            finally:
                if foreground:
                    self._front[route] -= 1
                    if not self._front[route]:
                        del self._front[route]
        try:
            yield
        finally:
            with self._condition:
                self._inflight.remove(route)
                self._condition.notify_all()

    def limited(self, route, error):
        now = self.now()
        hint = retry_after(error, now)
        with self.database.connect(write=True) as connection:
            row = connection.execute("SELECT strikes, retry_at FROM model_pressure WHERE route = ?",
                                     (route,)).fetchone()
            strikes = min((row["strikes"] if row else 0) + 1, 6)
            delay = min(1800, 60 * 2 ** (strikes - 1))
            retry_at = max(now + 1, hint) if hint is not None and math.isfinite(hint) else now + delay
            if row:
                retry_at = max(retry_at, row["retry_at"])
            connection.execute("INSERT OR REPLACE INTO model_pressure VALUES (?, ?, ?)",
                               (route, retry_at, strikes))
        return ModelPressureWait("model_rate_limited", retry_at)

    def succeeded(self, route):
        if route and self._cooldown(route):
            with self.database.connect(write=True) as connection:
                connection.execute("DELETE FROM model_pressure WHERE route = ? AND retry_at <= ?",
                                   (route, self.now()))
