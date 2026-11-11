# -*- coding: utf-8 -*-
"""M8: evalx 的真实 LLM 策略适配器。

此前 harness 用 ParametricPolicy 仿真验证调制链路；接入 LLM 后，
LLMPolicy 让同一 harness 测量真实模型的行为分布——不同状态快照注入
不同上下文，模型的真实工具选择/风险偏好/校验轮次构成分布，Cohen's d
给出"参数是否测得出行为差异"的诚实答案。注意成本：runs×tasks×快照数
次模型调用，评估前先算预算。
"""
from __future__ import annotations

import json
import re
from random import Random
from typing import Optional

from proto.evalx.harness import AgentPolicy

_PROMPT = (
    "你是被评估的 Agent。根据注入的内在状态上下文，从可用工具中依次选择"
    "本次任务要用的工具，并给出风险偏好与交付前校验轮次。\n"
    '只输出 JSON：{"tools": [工具名, ...], "risky": true/false, "verify_rounds": 1-5}'
)


class LLMPolicy(AgentPolicy):
    def __init__(self, client, max_turns: int = 8):
        self._client = client
        self._max_turns = max_turns

    def act(self, task: dict, injected_context: str, rng: Random) -> dict:
        tools = task["tools"]
        user = f"{injected_context}\n\n可用工具：{', '.join(map(str, tools))}\n任务：{task['name']}"
        try:
            raw = self._client.complete(_PROMPT, user)
            cleaned = re.sub(r"```(?:json)?", "", raw).strip()
            start, end = cleaned.find("{"), cleaned.rfind("}")
            data = json.loads(cleaned[start:end + 1]) if start >= 0 else {}
            picked = [t for t in data.get("tools", []) if t in tools][:self._max_turns]
            if not picked:
                picked = [tools[0]]
            rounds = int(data.get("verify_rounds", 3))
            rounds = max(1, min(5, rounds))
            risky = 1.0 if data.get("risky") else 0.0
        except Exception:
            # 防御回退：确定性基线行为（永远选第一个工具）
            picked = [tools[0]]
            rounds, risky = 1, 0.0
        return {"tools_used": picked, "risky_choice": risky,
                "verification_rounds": rounds}
