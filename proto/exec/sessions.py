"""Single-agent durable execution sessions, separate from Router checkpoints."""
from __future__ import annotations

import threading

from proto.exec.checkpoints import SCHEMA_VERSION
from proto.exec.plan_evidence import valid_approved_plan, valid_path, valid_text
from proto.exec.router import VARIANT_ID


SESSION_SCHEMA_VERSION = 2
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

    def abandon(self, identifier, reason):
        with self.lock:
            session = self.store.get("execution_sessions", identifier)
            if (session and self.compatible(session) and self.valid(session)
                    and session["status"] not in TERMINAL_STATES):
                session.update(status="cancelled", pending_decision=None,
                               result={"status": "cancelled", "reason": reason, "session_id": identifier})
                self.save(session)

    @staticmethod
    def compatible(session):
        return (type(session.get("schema_version")) is int
                and session["schema_version"] in (1, SESSION_SCHEMA_VERSION)
                and session.get("variant_id") == VARIANT_ID)

    @staticmethod
    def valid(session):
        if (not isinstance(session.get("id"), str) or not session["id"]
                or not isinstance(session.get("status"), str)
                or session.get("status") not in TERMINAL_STATES | {
                    "running", "resuming", "awaiting_approval", "needs_reconciliation", "waiting_model"}):
            return False
        context = session.get("context")
        if not isinstance(context, dict) or any(not isinstance(context.get(key), str) for key in (
                "profile", "owner_priority", "task", "acceptance", "pool", "goal_id", "workspace", "project_dir")):
            return False
        if context["pool"] not in ("owner_tasks", "self_initiated"):
            return False
        if "approved_plan" in context and (not valid_approved_plan(context["approved_plan"])
                or context["pool"] != "owner_tasks" or context["goal_id"]
                or not valid_text(context["acceptance"], 1000)
                or (context["project_dir"] and not valid_path(context["project_dir"]))):
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
        checks = session.get("local_checks", [])
        if not isinstance(checks, list) or len(checks) > 2 or any(check not in ("build", "test") for check in checks):
            return False
        if "prompt_blocks" in session and (not isinstance(session["prompt_blocks"], list)
                or len(session["prompt_blocks"]) != 4
                or any(not isinstance(block, str) for block in session["prompt_blocks"])):
            return False
        delivery = session.get("delivery")
        if delivery is not None and (not isinstance(delivery, dict)
                or delivery.get("stage") not in ("", "design", "build", "verify")
                or "start" not in delivery or not (delivery["start"] is None or isinstance(delivery["start"], str))):
            return False
        pending = session.get("pending_call")
        decision = session.get("pending_decision")
        if session.get("schema_version") == SESSION_SCHEMA_VERSION and "pending_decision" not in session:
            return False
        if decision is not None and (pending is not None or not isinstance(decision, dict)
                or type(decision.get("step")) is not int or not 0 <= decision["step"] == session["next_step"] - 1
                or not isinstance(decision.get("raw"), str)):
            return False
        if pending is not None:
            if (not isinstance(pending, dict) or type(pending.get("step")) is not int
                    or not 0 <= pending["step"] < session["next_step"]
                    or (session["schema_version"] == SESSION_SCHEMA_VERSION
                        and pending["step"] != session["next_step"] - 1)
                    or pending.get("task_id") != f"{session['id']}-{pending['step']}"
                    or pending["task_id"] not in session["task_ids"]
                    or not isinstance(pending.get("tool"), str)
                    or not isinstance(pending.get("args"), dict)
                    or not isinstance(pending.get("category"), str)):
                return False
        return session["status"] != "awaiting_approval" or pending is not None

    @staticmethod
    def completed_call_matches(session, state):
        pending, context = session["pending_call"], session["context"]
        task = state.get("task")
        if not isinstance(task, dict):
            return False
        return (type(state.get("step")) is int and state["step"] == 1
                and isinstance(state.get("results"), list)
                and task.get("description") == context["task"]
                and task.get("owner_assigned") is (context["pool"] == "owner_tasks")
                and task.get("autonomous") is session.get("autonomous")
                and task.get("steps") == [{key: pending[key] for key in ("tool", "args", "category")}])

    @classmethod
    def safe_to_continue(cls, session, checkpoints):
        if (not session or not cls.compatible(session) or not cls.valid(session)
                or session["schema_version"] != SESSION_SCHEMA_VERSION):
            return False
        status = session["status"]
        pending = session.get("pending_call")
        if pending is None:
            return status in {"running", "resuming", "waiting_model"}
        if (status not in {"running", "resuming", "awaiting_approval", "waiting_model"}
                and not (status == "needs_reconciliation"
                         and (session.get("result") or {}).get("reason") == "dispatcher_interrupted")):
            return False
        record = checkpoints.resume(pending["task_id"])
        if (not record or record.get("task_id") != pending["task_id"]
                or type(record.get("schema_version")) is not int or record["schema_version"] != SCHEMA_VERSION
                or record.get("variant_id") != VARIANT_ID):
            return False
        state = record.get("state")
        return (isinstance(state, dict) and state.get("status") == "completed"
                and not state.get("waiting_for") and cls.completed_call_matches(session, state))
