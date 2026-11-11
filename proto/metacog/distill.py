# -*- coding: utf-8 -*-
"""M4-③: 任务结果 → 策略蒸馏（设计 §7.3 机制三）。

确定性蒸馏规则（LLM 蒸馏器接入后替换为同接口）：
- 成功且有重试 → 正向策略："当 <域> 任务受挫时，重试并退避"；
- 失败/被拒 → 条件化教训（valence < 0）："当 <域> 时避免 <原因>"；
- 平凡成功 → 不蒸馏（防策略库噪声膨胀）。
"""
from __future__ import annotations

from typing import Optional

from proto.metacog.strategies import Strategy, StrategyLibrary


class TaskDistiller:
    def __init__(self, library: StrategyLibrary):
        self._lib = library

    def distill(self, result, domain: str = "general") -> Optional[Strategy]:
        if result.status == "completed":
            if getattr(result, "retries", 0) > 0:
                return self._lib.add(
                    f"当 {domain} 任务受挫时，重试并退避（已验证有效）",
                    cluster=domain, utility=0.55, valence=+0.1)
            return None
        if result.status in ("failed", "denied"):
            reason = getattr(result, "reason", None) or "未知原因"
            return self._lib.add(
                f"当 {domain} 时避免：{reason}",
                cluster=domain, utility=0.45, valence=-0.1)
        return None
