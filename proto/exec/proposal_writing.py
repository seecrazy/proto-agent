# -*- coding: utf-8 -*-
"""M34-①: 提议简明精要机制——主人一眼看懂它要做什么。

规则：提议 = 【做什么】一句 + 【为什么】一句 + 【产出什么】一句。
用 LLM 按此结构重写原始提议文本；离线时用确定性截取兜底。
"""

REWRITE_PROMPT = (
    "你是 PROTO 的提议撰写器。把下面的提议重写为 3 句话，严格按此格式：\n"
    "做什么：<一个具体动作，不超过 30 字>\n"
    "为什么：<一个理由，引用主人优先级或它自己的欲望，不超过 25 字>\n"
    "产出：<完成后主人会看到什么，可验证，不超过 25 字>\n"
    "禁止客套、禁止背景铺垫、禁止形容词堆砌。\n"
    "原始提议：{raw}\n"
    "只输出这 3 行，不要其他内容。"
)

MAX_LEN = 160


def rewrite_proposal(agent, raw: str) -> str:
    """LLM 重写提议为 3 句结构；失败时确定性截断兜底。"""
    fallback = raw[:MAX_LEN] + ("…" if len(raw) > MAX_LEN else "")
    if agent is None or getattr(agent, "llm_fast", None) is None:
        return fallback
    try:
        out = agent._complete(agent.llm_fast,
                              REWRITE_PROMPT.format(raw=raw[:400]),
                              raw[:200])
        out = out.strip()
        # 简单质量门：必须含"做什么"关键字，否则用兜底
        if "做什么" in out and len(out) < 300:
            return out
        return fallback
    except Exception:
        return fallback
