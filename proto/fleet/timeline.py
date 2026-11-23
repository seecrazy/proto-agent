# -*- coding: utf-8 -*-
"""Run timeline projection——把执行会话投影成主人可读的逐步记录。

数据源是执行会话（execution_sessions）与 runs 表：session 存 trace/events/context，
runs 存调度代次与卡点。同一 session 跨重试存活——「第 N 次调度」+ session 短标识
就能回答「这是老的 24 步在续跑，还是全新的 24 步」。
"""
from __future__ import annotations

from pathlib import Path

from proto.exec.work_context import target_args


def _brief(value, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _args_brief(arguments) -> str:
    try:
        return _brief(target_args(arguments if isinstance(arguments, dict) else {}), 120)
    except Exception:
        return ""


def _steps(trace, limit: int = 48) -> list:
    rows = []
    for entry in trace[-limit:]:
        if not isinstance(entry, dict):
            continue
        rows.append({
            "step": entry.get("step"),
            "tool": str(entry.get("tool") or "?"),
            "category": entry.get("category") or "",
            "note": _brief(entry.get("note"), 90),
            "args": _args_brief(entry.get("args")),
            "result": _brief(entry.get("result"), 200),
            "ok": entry.get("ok", True) is not False,
            "written": entry.get("written_path") or "",
            "ts": entry.get("ts"),
        })
    return rows


def _journal_tail(session: dict) -> dict:
    path = session.get("journal_path") or ""
    if not path:
        return {}
    try:
        from proto.work.journal import GoalJournal
        latest = GoalJournal(Path(path)).latest_round()
    except OSError:
        return {}
    return latest or {}


def run_timeline(runs, run_id: str, agent) -> dict:
    """组装单条 run 的执行时间线（run → 会话 → 逐步 trace + 意图 + 接力日志）。"""
    run = runs.get(run_id)
    result = run.get("result") or {}
    session_id = run.get("session_id") or result.get("session_id") or ""
    session = agent.sessions.lookup(session_id) if session_id else None
    if not isinstance(session, dict):
        session = {}
    context = session.get("context") or {}
    trace = session.get("trace") or result.get("trace") or []
    with runs.database.connect() as connection:
        prev = connection.execute(
            "SELECT session_id FROM runs WHERE agent_id = ? AND seq < "
            "(SELECT seq FROM runs WHERE id = ?) ORDER BY seq DESC LIMIT 1",
            (agent.fleet_id, run_id)).fetchone()
    prev_session = (prev["session_id"] or "") if prev else ""
    return {
        "run": {"id": run["id"], "kind": run["kind"], "status": run["status"],
                "reason": run["reason"], "generation": run["generation"],
                "created_at": run["created_at"], "updated_at": run["updated_at"],
                "retry_at": result.get("retry_at"),
                "model_reason": result.get("model_reason"),
                "restart_attempts": result.get("restart_attempts", 0),
                "resumed": result.get("resume_session") is True,
                "session_id": session_id},
        "chain": {"attempt": run["generation"],
                  "same_as_previous": bool(session_id and prev_session == session_id),
                  "previous_session": prev_session,
                  "restarts": result.get("restart_attempts", 0)},
        "intent": {"task": _brief(context.get("task"), 240),
                   "goal_id": context.get("goal_id") or result.get("goal_id") or "",
                   "project_dir": context.get("project_dir") or result.get("project_dir") or "",
                   "pool": context.get("pool") or "",
                   "acceptance": _brief(context.get("acceptance"), 160)},
        "progress": {"next_step": session.get("next_step") or 0,
                     "max_steps": session.get("max_steps") or 0,
                     "session_status": session.get("status") or ""},
        "journal": _journal_tail(session),
        "steps": _steps(trace),
        "outcome": {"status": result.get("status") or "",
                    "reason": result.get("reason") or "",
                    "summary": _brief(result.get("summary"), 200),
                    "verified": result.get("verified"),
                    "missing": (result.get("missing") or [])[:3]},
    }
