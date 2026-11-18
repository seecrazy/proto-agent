# -*- coding: utf-8 -*-
"""M23: 独立验证门——"做完了"与"做对了"分离（质量三定律之三）。

研究依据：验证器只看执行者自述时错误接受率 18-22%；给它**原始验收条件**后
降到 2%。因此本门的设计约束：
- 输入 = 验收定义 + 结构化轨迹 + 产物原文，**绝不包含执行者的完成叙述**；
- 独立干净上下文：全新 LLM 调用，看不到循环中形成的任何叙事；
- 失败关闭：解析失败/无验证器即不通过（离线场景显式跳过而非放行）。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import List, Optional

ACCEPTANCE_PROMPT = (
    "为下面的任务给出**最小可检验定义**（什么场景、做什么操作、观察到什么结果才算做对；"
    "优先可用运行结果判定的标准）。\n"
    "可达性原则：验收必须全部在本机工作区可达成——能写出的文件、能用 run_python 跑出"
    "结果的脚本/测试；需要主人账号/设备/外部服务（Xcode 真机安装、TestFlight、"
    "App Store 提交、付费、发邮件等）的事项不得作为验收项，应表述为'产出清单文档'"
    "这类本地可验证的形式。\n"
    "写不出可检验标准的任务请返回空。"
    '只输出 JSON：{"acceptance": "..."}，空则 {"acceptance": ""}。'
)

VERIFIER_PROMPT = (
    "你是独立验收员（质量门），与执行者物理隔离——你看到的只有下列材料，"
    "没有也不需要执行者的任何叙述。\n"
    "任务：{task}\n"
    "验收定义（什么算做对）：{acceptance}\n"
    "执行轨迹（工具调用与原始输出，客观证据）：\n{trace}\n"
    "产物（写入的文件原文）：\n{artifacts}\n"
    "规则：**失败关闭**——验收定义的每一条都必须能在轨迹或产物中找到对应证据，"
    "否则判 fail 并列出缺少什么。不允许因为'看起来差不多'而通过。"
    '只输出 JSON：{{"verdict": "pass"|"fail", "missing": ["缺少的证据…"]}}'
)


@dataclass
class Verdict:
    passed: bool
    missing: List[str] = field(default_factory=list)
    reason: str = ""


def generate_acceptance(llm, task_text: str, complete=None) -> str:
    """想法出关门（定律一）：无验收定义不进入验证执行。失败/离线返回空。"""
    if llm is None:
        return ""
    try:
        raw = (complete(llm, ACCEPTANCE_PROMPT, task_text)
               if complete is not None else llm.complete(ACCEPTANCE_PROMPT, task_text))
        cleaned = re.sub(r"```(?:json)?", "", raw).strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        data = json.loads(cleaned[start:end + 1])
        return str(data.get("acceptance", "")).strip()[:300]
    except Exception:
        return ""


def verify(task_text: str, acceptance: str, trace: List[dict],
           artifacts: str, llm, complete=None) -> Optional[Verdict]:
    """独立验证。返回 None=门不适用（无验收定义/离线，显式跳过）；
    返回 Verdict 则必为明确判定（解析失败 → 失败关闭）。"""
    if not acceptance.strip():
        return None
    if llm is None:
        return None
    trace_lines = "\n".join(
        f"{i+1}. [{t.get('tool')}] 参数={json.dumps(t.get('args', {}), ensure_ascii=False)[:150]}"
        f" → 结果={str(t.get('result', ''))[:200]}"
        for i, t in enumerate(trace))
    prompt = VERIFIER_PROMPT.format(
        task=task_text, acceptance=acceptance, trace=trace_lines or "（无）",
        artifacts=(artifacts or "（无）")[:3000])
    try:
        system = "你是独立验收员（质量门）。" + (
            prompt.split("你是独立验收员（质量门），", 1)[1]
            if "你是独立验收员（质量门），" in prompt else prompt)
        raw = (complete(llm, system, prompt)
               if complete is not None else llm.complete(system, prompt))
    except Exception as exc:
        return Verdict(False, ["验证器调用失败"], f"llm_error:{exc}")
    try:
        cleaned = re.sub(r"```(?:json)?", "", raw).strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        data = json.loads(cleaned[start:end + 1])
        verdict = data.get("verdict", "fail")
        missing = [str(m)[:200] for m in data.get("missing", [])][:5]
        if verdict == "pass":
            return Verdict(True)
        return Verdict(False, missing or ["未给出缺少项"], "evidence_missing")
    except Exception:
        return Verdict(False, ["验证器输出不可解析"], "parse_error")


def artifacts_from_trace(trace: List[dict]) -> str:
    """从轨迹提取产物原文（write_file 的内容即客观产物）。"""
    parts = []
    for t in trace:
        if t.get("tool") in ("write_file", "edit_file"):
            args = t.get("args", {})
            parts.append(f"--- {args.get('path', '?')} ---\n"
                         f"{str(args.get('content', ''))[:800]}")
    return "\n".join(parts)[:4000]
