"""Bounded, public execution facts; never log prompt, parameters or tool output."""
from __future__ import annotations

import re
import threading
import time
from collections import defaultdict, deque
from pathlib import PurePosixPath


RUN_LABELS = {
    "tick": "进行后台自主检查", "act": "执行自主行动", "task": "处理你指派的任务",
    "plan": "生成只读计划", "planned_task": "执行你批准的计划",
    "work": "处理项目工作", "review": "评审项目产物",
    "resume": "继续已批准的步骤", "accept": "处理已批准的提案", "deny": "处理拒绝决定",
}

TERMINAL_LABELS = {"succeeded": "结束", "failed": "失败", "blocked": "受阻", "cancelled": "取消"}

REASONS = {
    "model_timeout": "模型响应超时", "model_forbidden": "模型端拒绝请求（403）",
    "model_unauthorized": "模型身份校验失败（401）", "model_rate_limited": "模型端限流（429）",
    "model_not_configured": "未配置可用模型", "model_unreachable": "无法连接模型端",
    "model_http_error": "模型端返回 HTTP 错误", "model_request_failed": "模型请求失败",
    "model_server_error": "模型端暂时不可用",
    "model_not_found": "模型接入找不到（404）", "model_group_unavailable": "模型组暂不可用",
    "model_route_busy": "同一模型接入正在服务另一条请求",
    "circuit_breaker_open": "模型暂在熔断冷却", "run_cancelled": "主人已请求取消",
    "fleet_paused": "Agent 已暂停", "muted": "Agent 已静音",
    "manual_only": "当前为手动模式", "not_enabled": "自主行动已关闭", "suggestions_only": "当前仅提案",
    "budget": "额度不足", "verification": "尚未通过验收",
    "needs_reconciliation": "在途效果未知，需人工核对",
    "model_retry_wait": "原进度已保存，等待重试模型",
    "interrupted_requeued": "重启后从已知断点续作", "safe_resume_ready": "原任务已重新排队",
    "model_retry_exhausted": "模型自动重试已用尽，原任务可手动继续",
    "safe_resume_model": "模型或额度受阻，修复后可继续原任务",
    "safe_resume_limit": "已保留安全断点，等待主人继续",
    "context_budget_exceeded": "完整任务指令超过执行上下文上限",
    "delivery_no_progress": "连续两个工作片无实质文件变化，已暂停",
    "delivery_not_verified": "四个工作片仍未通过验收，已暂停",
    "delivery_predecessor_pending": "等待前一阶段验收",
    "project_directory_unavailable": "项目目录不可用，修复后可恢复目标",
    "ios_delivery_evidence_missing": "缺少真实且未过期的模拟器构建或测试证据",
    "local_verification_evidence_missing": "缺少本机编译或测试回执，尚未验收",
}


def _reason(raw):
    if raw in REASONS:
        return raw
    reason = (raw or "").lower()
    if "circuit_breaker_open" in reason:
        return "circuit_breaker_open"
    if "budget" in reason:
        return "budget"
    if reason.startswith("llm_error:"):
        code = reason.removeprefix("llm_error:")
        if code in REASONS:
            return code
        if "timed out" in reason or "timeout" in reason:
            return "model_timeout"
        if "403" in reason:
            return "model_forbidden"
        if "401" in reason:
            return "model_unauthorized"
        if "429" in reason:
            return "model_rate_limited"
        return "model_request_failed"
    return "verification" if "verifi" in reason else ""


def _line(value, limit):
    return " ".join(str(value or "").split())[:limit]


def artifact_path(path):
    if not isinstance(path, str) or not 0 < len(path) <= 240 or path != _line(path, 240) or "\\" in path:
        return ""
    item = PurePosixPath(path)
    return path if len(item.parts) > 1 and item.parts[0] == "projects" and ".." not in item.parts else ""


class RunActivity:
    def __init__(self, database):
        self.database = database
        self._events = defaultdict(lambda: deque(maxlen=32))
        self._lock = threading.Lock()
        self._seq = 0

    def record(self, lease, phase, *, tool="", path="", reason="", model=""):
        marker = getattr(lease.agent, "_current_op", None) or {}
        with self._lock:
            self._seq += 1
            self._events[lease.agent.fleet_id].append({
                "seq": self._seq, "at": time.time(), "run_id": lease.id,
                "phase": phase, "step": marker.get("step") or 0,
                "tool": tool if re.fullmatch(r"[\w-]{1,64}", tool) else "",
                "path": artifact_path(path), "reason": _reason(reason),
                "model": _line(model, 80),
            })

    def forget(self, agent_id):
        with self._lock:
            self._events.pop(agent_id, None)

    def snapshot(self, agent):
        with self.database.connect() as connection:
            fields = """id, kind, status, reason, updated_at, cancel_requested,
                    coalesce(json_extract(result, '$.verified'), json_extract(result, '$.proposal.verified')) AS verified"""
            active = connection.execute(f"""
                SELECT {fields} FROM runs WHERE agent_id = ? AND status IN
                   ('running', 'needs_reconciliation', 'awaiting_approval', 'queued')
                ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'needs_reconciliation' THEN 1
                   WHEN 'awaiting_approval' THEN 2 ELSE 3 END, seq DESC LIMIT 1
            """, (agent.fleet_id,)).fetchone()
            last = connection.execute(f"""
                SELECT {fields} FROM runs WHERE agent_id = ? AND status IN
                   ('succeeded', 'failed', 'blocked', 'cancelled')
                   AND NOT (kind = 'tick' AND status = 'succeeded' AND json_extract(result, '$.proposal') IS NULL)
                ORDER BY seq DESC LIMIT 1
            """, (agent.fleet_id,)).fetchone()
        run, previous = (dict(active) if active else None), (dict(last) if last else None)
        for row in (run, previous):
            if row:
                row["reason"] = _reason(row["reason"])
        marker = getattr(agent, "_current_op", None) or {}
        with self._lock:
            events = list(self._events.get(agent.fleet_id, ()))
        current = next((event for event in reversed(events)
                        if run and event["run_id"] == run["id"]), None)
        return {"run": run, "last_run": previous, "current": current, "events": events,
                "goal": _line(marker.get("goal"), 80), "step": marker.get("step") or 0,
                "max_steps": marker.get("max_steps") or 0, "model": agent.model_status(),
                "autonomy": _reason(agent.autonomy_blocker())}


def activity_reply(snapshot, *, detailed=False):
    run, model = snapshot["run"], snapshot["model"]
    text = "我目前没有正在运行的后台任务。"
    if (run and run["status"] == "running") or snapshot["goal"]:
        label = RUN_LABELS.get(run["kind"] if run else "", "处理后台任务")
        text = (f"我正在处理：{snapshot['goal']}。" if snapshot["goal"] else f"我正在{label}。")
        if snapshot["step"] and snapshot["max_steps"]:
            text += f"第 {snapshot['step']}/{snapshot['max_steps']} 步。"
        current = snapshot["current"] or {}
        if current.get("phase") == "model_wait":
            name = current.get("model")
            text += f"正在等模型「{name}」返回。" if name else "正在等模型返回。"
        if current.get("phase") == "tool_start":
            text += f"正在执行工具 {current['tool']}。"
        if run and run["cancel_requested"]:
            text += "主人已请求取消，当前在途步骤未必能撤回。"
    elif run:
        if run.get("reason") in ("model_retry_wait", "interrupted_requeued", "safe_resume_ready",
                                  "model_retry_exhausted", "safe_resume_model", "safe_resume_limit"):
            text += REASONS[run["reason"]] + "。"
        else:
            text += {"awaiting_approval": "有任务等待主人批准。", "queued": "有任务正在排队。",
                     "needs_reconciliation": "有在途结果未知，需人工核对。"}.get(run["status"], "")
    if model["waiting"]:
        if model["reason"] == "model_route_busy":
            text += "同一模型接入正在处理另一条请求。"
        elif model["reason"] == "model_rate_limited":
            text += "供应商限流，此接入按冷却截止等待，不会重复发送。"
        else:
            text += "模型处于熔断冷却或正在做恢复试探。"
    reason = REASONS.get(model["reason"]) or REASONS.get((snapshot["last_run"] or {}).get("reason"))
    if reason:
        text += f"最近的卡点：{reason}。"
    if snapshot["autonomy"]:
        text += REASONS[snapshot["autonomy"]] + "。"
    paths = list(dict.fromkeys(event["path"] for event in reversed(snapshot["events"]) if event["path"]))[:3]
    if paths:
        text += "最近写入：" + "、".join(paths) + "（写入不等于验收）。"
    if detailed:
        last = snapshot["last_run"]
        if last:
            label = RUN_LABELS.get(last["kind"], "处理后台任务")
            text += (f"最近后台轮次：{label}，{TERMINAL_LABELS.get(last['status'], '状态未知')}"
                     + ("，已通过验收。" if last["verified"] else "，无通过验收记录。"))
        elif not paths:
            text += "暂无已结束的后台工作记录。"
    return text
