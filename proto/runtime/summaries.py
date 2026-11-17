# -*- coding: utf-8 -*-
"""M32: 时间维度汇总——时/天/周/月/年关键行为聚合。"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List

WINDOWS = [("hour", 3600.0), ("day", 86400.0), ("week", 7 * 86400.0),
           ("month", 30 * 86400.0), ("year", 365 * 86400.0)]


def _parse(ts) -> float:
    if isinstance(ts, (int, float)):
        return float(ts)
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def bucket_events(events: List[dict], now: float = None) -> Dict[str, dict]:
    """按 时/天/周/月/年 聚合：总数 + 各事件类型计数。"""
    import time as _time
    now = now if now is not None else _time.time()
    out = {}
    for name, window in WINDOWS:
        counts = Counter(e["event_type"] for e in events
                         if now - _parse(e.get("ts", 0)) <= window)
        out[name] = {"total": sum(counts.values()), "by_type": dict(counts)}
    return out
