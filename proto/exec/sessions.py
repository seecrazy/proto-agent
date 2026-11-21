"""Single-agent durable execution sessions, separate from Router checkpoints."""
from __future__ import annotations

import threading

from proto.exec.router import VARIANT_ID


SESSION_SCHEMA_VERSION = 1
TERMINAL_STATES = {"verified", "completed", "awaiting_retry", "max_steps",
                   "failed", "denied", "cancelled", "no_llm"}


class ExecutionSessions:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()

    def save(self, session):
        self.store.put("execution_sessions", session["id"], session)

    def lookup(self, identifier):
        record = self.store.get("execution_sessions", identifier)
        if record is not None:
            return record
        matches = self.store.find("execution_sessions", lambda candidate:
                                  identifier in candidate.get("task_ids", []))
        return matches[0] if matches else None

    def pending_for_goal(self, goal_id):
        matches = self.store.find("execution_sessions", lambda candidate: self._pending_goal(candidate) == goal_id)
        return matches[0] if matches else None

    @staticmethod
    def _pending_goal(candidate):
        context, state = candidate.get("context"), candidate.get("status")
        if isinstance(context, dict) and (not isinstance(state, str) or state not in TERMINAL_STATES):
            goal_id = context.get("goal_id")
            if isinstance(goal_id, str) and goal_id:
                return goal_id
        return None

    def pending_goal_ids(self):
        return {self._pending_goal(session) for session in self.store.find("execution_sessions", self._pending_goal)}

    @staticmethod
    def compatible(session):
        return (type(session.get("schema_version")) is int
                and session["schema_version"] == SESSION_SCHEMA_VERSION
                and session.get("variant_id") == VARIANT_ID)

    @staticmethod
    def valid(session):
        if (not isinstance(session.get("id"), str) or not session["id"]
                or not isinstance(session.get("status"), str)
                or session.get("status") not in TERMINAL_STATES | {
                    "running", "resuming", "awaiting_approval", "needs_reconciliation"}):
            return False
        context = session.get("context")
        if not isinstance(context, dict) or any(not isinstance(context.get(key), str) for key in (
                "profile", "owner_priority", "task", "acceptance", "pool", "goal_id", "workspace", "project_dir")):
            return False
        if context["pool"] not in ("owner_tasks", "self_initiated"):
            return False
        if "autonomous" in session and type(session["autonomous"]) is not bool:
            return False
        instructions, active = context.get("instructions"), context.get("active_instructions")
        if not isinstance(instructions, dict) or not isinstance(active, list):
            return False
        if any(not isinstance(name, str) or name not in instructions for name in active):
            return False
        if any(not isinstance(item, dict) or not isinstance(item.get("text"), str)
               or type(item.get("bytes")) is not int or item["bytes"] < 0
               or not isinstance(item.get("sha256"), str) for item in instructions.values()):
            return False
        for key in ("max_steps", "next_step", "explore_run"):
            if type(session.get(key)) is not int or session[key] < 0:
                return False
        if session["next_step"] > session["max_steps"]:
            return False
        for key, item_type in (("observations", str), ("trace", dict), ("events", dict),
                               ("last_sigs", str), ("task_ids", str)):
            if not isinstance(session.get(key), list) or any(not isinstance(item, item_type) for item in session[key]):
                return False
        if any(not isinstance(session.get(key), str) for key in ("feedback", "journal_path")):
            return False
        if "prompt_blocks" in session and (not isinstance(session["prompt_blocks"], list)
                or len(session["prompt_blocks"]) != 4
                or any(not isinstance(block, str) for block in session["prompt_blocks"])):
            return False
        pending = session.get("pending_call")
        if pending is not None:
            if (not isinstance(pending, dict) or type(pending.get("step")) is not int
                    or not 0 <= pending["step"] < session["next_step"]
                    or pending.get("task_id") != f"{session['id']}-{pending['step']}"
                    or pending["task_id"] not in session["task_ids"]
                    or not isinstance(pending.get("tool"), str)
                    or not isinstance(pending.get("args"), dict)
                    or not isinstance(pending.get("category"), str)):
                return False
        return session["status"] != "awaiting_approval" or pending is not None
