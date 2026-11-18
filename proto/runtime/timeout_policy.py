# -*- coding: utf-8 -*-
"""M38: 主人超时自主裁决。

用户诉求：'问主人，主人一段时间不回复，它们应该可以自主决定并采取
行动，除非是什么高危或需要较高权限的事。'

裁决规则（从上到下，命中即止）：
1. IRREVERSIBLE —— 高危不可逆，永远只等主人；
2. 工具声明 owner_only=True —— 较高权限（外部执行/未隔离代码执行），
   永远只等主人；
3. 工作区内的可见晋升（write_file→visible）—— 本地文件、可回滚，
   构造性低风险，LLM 在线时补一道复核，离线也放行；
4. 其余 visible_write —— LLM 风险评估：low 才放行，medium/high 或
   离线一律继续等主人；
5. 全部放行都要先过当日自主配额（AutoApproveQuota，默认 3 次/天，
   UI 可调，0=关）——自主是有界的，不是无限代批。

每次裁决（无论批与不批）都写审计并给主人收件箱留言，事后可完整追溯。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

RISK_PROMPT = (
    "你是 PROTO 的自主裁决器。主人已 {minutes} 分钟未回应一个待批动作，"
    "评估在没有主人批准的情况下自行执行它的风险。\n"
    "动作：{description}\n"
    "细节：{payload}\n"
    '只输出 JSON：{{"risk": "low"|"medium"|"high", "reason": "一句话"}}\n'
    "判断标准：只影响本机工作区文件/可回滚=low；"
    "改动可见产物或有外部可见副作用=medium；"
    "不可逆/花钱/动权限/外部执行=high。"
)


@dataclass
class TimeoutDecision:
    action: str        # auto_approve | keep_waiting
    reason: str


def _proposal_tool(proposal) -> Optional[str]:
    payload = proposal.payload or {}
    step = payload.get("step") or {}
    return step.get("tool")


def decide(proposal, tools, llm, complete, minutes: int) -> TimeoutDecision:
    """tools=ToolRegistry（查 owner_only）；llm/complete 为风险评估通道。"""
    from proto.kernel.kernel import ActionCategory
    if proposal.category is ActionCategory.IRREVERSIBLE:
        return TimeoutDecision("keep_waiting", "irreversible_requires_owner")
    tool = _proposal_tool(proposal)
    if tool:
        if tool == "write_file":
            pass           # 工作区文件写=构造性低风险，走下方构造性判定
        else:
            try:
                if tools.policy(tool).owner_only:
                    return TimeoutDecision(
                        "keep_waiting", f"elevated_permission_requires_owner:{tool}")
            except KeyError:
                return TimeoutDecision("keep_waiting", f"unknown_tool:{tool}")
    payload_str = "；".join(f"{k}={str(v)[:60]}" for k, v
                           in (proposal.payload or {}).items()) or "（晋升工作区产物）"
    import json as _json
    import re as _re
    risk, reason = "low", "工作区内可见晋升（本地文件、可回滚）"
    if tool is not None:   # 非晋升类动作用 LLM 评估，离线保守
        if llm is None:
            return TimeoutDecision("keep_waiting", "offline_conservative")
        try:
            raw = complete(llm, RISK_PROMPT.format(
                minutes=minutes, description=proposal.description[:120],
                payload=payload_str[:200]), proposal.description)
            cleaned = _re.sub(r"```(?:json)?", "", raw).strip()
            start, end = cleaned.find("{"), cleaned.rfind("}")
            data = _json.loads(cleaned[start:end + 1])
            risk = str(data.get("risk", "high")).lower()
            reason = str(data.get("reason", ""))[:120]
        except Exception:
            return TimeoutDecision("keep_waiting", "risk_assess_failed")
    if risk != "low":
        return TimeoutDecision("keep_waiting", f"risk_{risk}:{reason}")
    return TimeoutDecision("auto_approve", f"risk_low:{reason}")
