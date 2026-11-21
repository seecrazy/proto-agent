"""Durable dispatch, serialized Agent writers, and conservative restart recovery."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from uuid import uuid4

from proto.exec.checkpoints import SCHEMA_VERSION
from proto.exec.router import VARIANT_ID
from proto.exec.sessions import TERMINAL_STATES
from proto.runtime.coordination import CoordinationDB, CoordinationError, encode
from proto.runtime.fleet_budget import FleetBudget
from proto.runtime.projects import ProjectDelivery
from proto.runtime.runcontext import current_run
from proto.runtime.task_plans import PLAN_EXEC_KEY, TaskPlans
from proto.system.instance_lock import InstanceLock


_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


def _record(row, *, summary=False):
    if row is None:
        return None
    result = dict(row)
    result.pop("request_key")
    result["cancel_requested"] = bool(result["cancel_requested"])
    for key in ("args", "waiting", "decision", "result"):
        if summary and key == "result":
            result.pop(key)
        elif result[key] is not None:
            result[key] = json.loads(result[key])
    return result


class RunLease:
    def __init__(self, coordinator, run, agent):
        self.coordinator = coordinator
        self.id = run["id"]
        self.generation = run["generation"]
        self.agent = agent
        self.project = bool(run["work_id"])
        self.stopped = False

    def reason(self):
        with self.coordinator.database.connect() as connection:
            row = connection.execute("SELECT status, generation, cancel_requested FROM runs WHERE id = ?",
                                     (self.id,)).fetchone()
            if (row is None or row["status"] != "running" or row["generation"] != self.generation
                    or row["cancel_requested"]):
                self.stopped = True
                return "run_cancelled"
        if self.project:
            reason = self.agent.autonomy_view()["blocked_reason"]
            if reason in ("fleet_paused", "muted"):
                return reason
        return ""

    def link(self, *, session_id=None, checkpoint_id=None):
        with self.coordinator.database.connect(write=True) as connection:
            changed = connection.execute("""
                UPDATE runs SET session_id = coalesce(?, session_id),
                    checkpoint_id = CASE WHEN ? IS NOT NULL THEN NULL ELSE coalesce(?, checkpoint_id) END
                WHERE id = ? AND status = 'running' AND generation = ?
            """, (session_id, session_id, checkpoint_id, self.id, self.generation))
            if not changed.rowcount:
                raise CoordinationError("stale_run_claim")


class RunCoordinator:
    def __init__(self, manager):
        self.manager = manager
        self.database = CoordinationDB(manager.root)
        self.budget = FleetBudget(self.database)
        self.projects = ProjectDelivery(self)
        self.task_plans = TaskPlans(self)
        self._instance = InstanceLock(self.database.directory)
        self._stop = threading.Event()
        self._condition = threading.Condition()
        self._lifecycle_lock = threading.Lock()
        self._pick_lock = threading.Lock()
        self._workers = []
        self._live = 0

    def notify(self):
        with self._condition:
            self._condition.notify_all()

    def enqueue(self, agent_id, kind, args=None, *, key=None):
        if self.manager.info(agent_id) is None:
            raise CoordinationError("no_such_agent", 404)
        if key is not None and (not isinstance(key, str) or not _KEY.fullmatch(key)
                                or key.startswith(PLAN_EXEC_KEY)):
            raise CoordinationError("invalid_idempotency_key", 400)
        with self.database.connect(write=True) as connection:
            record = self.insert(connection, agent_id, kind, args or {}, key=key)
        self.notify()
        return record

    @staticmethod
    def insert(connection, agent_id, kind, args, *, key=None, work_id=None, transfer_from=None):
        serialized = encode(args)
        if key is not None:
            prior = connection.execute("SELECT * FROM runs WHERE agent_id = ? AND request_key = ?",
                                       (agent_id, key)).fetchone()
            if prior:
                if (prior["kind"], prior["args"]) != (kind, serialized):
                    raise CoordinationError("idempotency_conflict")
                return _record(prior)
        if kind == "tick" and key is None and not args:
            prior = connection.execute("""
                SELECT * FROM runs WHERE agent_id = ? AND kind = 'tick' AND status IN ('queued', 'running')
                AND decision IS NULL AND args = '{}' LIMIT 1
            """, (agent_id,)).fetchone()
            if prior:
                return _record(prior)
        pending = connection.execute("""
            SELECT count(*), count(CASE WHEN agent_id = ? THEN 1 END)
            FROM runs WHERE status IN ('queued', 'running', 'awaiting_approval', 'needs_reconciliation')
            AND id != coalesce(?, '')
        """, (agent_id, transfer_from)).fetchone()
        if pending[0] >= 4096 or (transfer_from is None and pending[1] >= 256):
            raise CoordinationError("run_queue_full", 429)
        run_id, now = uuid4().hex, time.time()
        connection.execute("""
            INSERT INTO runs (id, agent_id, kind, args, request_key, work_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (run_id, agent_id, kind, serialized, key, work_id, now, now))
        return _record(connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone())

    def get(self, run_id):
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise CoordinationError("no_such_run", 404)
        return _record(row)

    def list(self, agent_id=None, *, limit=60, before=None, active=False, reviewable=False):
        with self.database.connect() as connection:
            rows = connection.execute("""
                SELECT * FROM runs WHERE (? IS NULL OR agent_id = ?) AND (? IS NULL OR seq < ?)
                AND (? = 0 OR status IN ('queued', 'running', 'awaiting_approval', 'needs_reconciliation'))
                AND (? = 0 OR (kind = 'plan' AND status = 'succeeded' AND NOT EXISTS (
                    SELECT 1 FROM runs execution WHERE execution.agent_id = runs.agent_id
                    AND execution.request_key = ? || runs.id)))
                ORDER BY seq DESC LIMIT ?
            """, (agent_id, agent_id, before, before, int(active), int(reviewable), PLAN_EXEC_KEY, limit)).fetchall()
        return [dict(_record(row, summary=True), blocked_reason=self.blocked_reason(row)) for row in rows]

    def blocked_reason(self, row):
        if row["status"] != "queued":
            return row["reason"]
        info = self.manager.info(row["agent_id"])
        if info is None:
            return "agent_unavailable"
        with self.database.connect() as connection:
            if connection.execute("SELECT 1 FROM runs WHERE agent_id = ? AND status = 'needs_reconciliation' LIMIT 1",
                                  (row["agent_id"],)).fetchone():
                return "agent_needs_reconciliation"
            if row["work_id"]:
                dependency = self.projects.blocked(connection, row["work_id"])
                if dependency:
                    return dependency
        if row["work_id"] or (row["kind"] == "tick" and not row["decision"] and row["args"] in ("{}", {})):
            if info.get("paused"):
                return "fleet_paused"
        if row["work_id"]:
            cached = self.manager.get(row["agent_id"])
            if cached and cached.kernel.muted:
                return "muted"
        return ""

    def find_link(self, agent_id, identifier):
        if not identifier:
            return None
        with self.database.connect() as connection:
            rows = connection.execute("""
                SELECT * FROM runs WHERE agent_id = ? AND
                (session_id = ? OR checkpoint_id = ? OR waiting IS NOT NULL)
                ORDER BY seq DESC
            """, (agent_id, identifier, identifier)).fetchall()
        for row in rows:
            waiting = json.loads(row["waiting"]) if row["waiting"] else {}
            if identifier in (row["session_id"], row["checkpoint_id"], waiting.get("task_id"),
                              waiting.get("proposal_id")):
                return _record(row)
        return None

    @staticmethod
    def _decision(connection, run, approved, *, cancel=False):
        decision = dict(run["waiting"], approved=approved, generation=run["generation"])
        connection.execute("""
            UPDATE runs SET status = 'queued', decision = ?, cancel_requested = ?, reason = '', updated_at = ?
            WHERE id = ?
        """, (encode(decision), int(cancel), time.time(), run["id"]))

    def decide(self, run_id, approved, generation):
        if type(approved) is not bool or type(generation) is not int:
            raise CoordinationError("invalid_approval_decision", 400)
        with self.database.connect(write=True) as connection:
            run = self._get_locked(connection, run_id)
            decision = run["decision"] or {}
            if run["status"] != "awaiting_approval" and (decision.get("generation"), decision.get("approved")) == (
                    generation, approved):
                return run
            if run["status"] != "awaiting_approval" or run["generation"] != generation:
                raise CoordinationError("run_not_waiting_or_stale_generation")
            self._decision(connection, run, approved)
            record = self._get_locked(connection, run_id)
        self.notify()
        return record

    @staticmethod
    def _get_locked(connection, run_id):
        row = connection.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise CoordinationError("no_such_run", 404)
        return _record(row)

    def cancel(self, run_id):
        with self.database.connect(write=True) as connection:
            run = self._get_locked(connection, run_id)
            status = run["status"]
            if status == "awaiting_approval" or (status == "queued" and run["decision"]):
                self._decision(connection, run, False, cancel=True)
            elif status == "running":
                connection.execute("UPDATE runs SET cancel_requested = 1, updated_at = ? WHERE id = ?",
                                   (time.time(), run_id))
            elif status == "queued":
                self._save_result(connection, run, "cancelled", "run_cancelled",
                                  {"status": "cancelled", "reason": "run_cancelled"})
            else:
                return run
            record = self._get_locked(connection, run_id)
        self.notify()
        return record

    def close_reconciliation(self, run_id, generation):
        with self.database.connect(write=True) as connection:
            run = self._get_locked(connection, run_id)
            if (type(generation) is not int or run["status"] != "needs_reconciliation"
                    or run["generation"] != generation):
                raise CoordinationError("run_not_reconciling_or_stale_generation")
            self._save_result(connection, run, "cancelled", "owner_closed_unknown_effect",
                              {"status": "cancelled", "reason": "owner_closed_unknown_effect",
                               "unresolved": run["result"]})
            record = self._get_locked(connection, run_id)
        self.notify()
        return record

    def start(self, workers=2):
        with self._lifecycle_lock:
            self._start(workers)

    def _start(self, workers):
        if type(workers) is not int or not 1 <= workers <= 16:
            raise ValueError("invalid_worker_count")
        self.database.initialize()
        if self._live or not self._instance.acquire():
            raise CoordinationError("fleet_dispatcher_already_running")
        try:
            self.recover()
        except BaseException:
            self._instance.release()
            raise
        self._stop.clear()
        with self._condition:
            self._live = workers
            self._workers = [threading.Thread(target=self._worker, daemon=True, name=f"proto-run-{index}")
                             for index in range(workers)]
            for worker in self._workers:
                worker.start()

    def stop(self, timeout=1.0):
        with self._lifecycle_lock:
            self._stop.set()
            self.notify()
            deadline = time.monotonic() + timeout
            for worker in self._workers:
                worker.join(max(0.0, deadline - time.monotonic()))

    def wait(self, run_id, *, timeout=20.0):
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                run = self.get(run_id)
                remaining = deadline - time.monotonic()
                if run["status"] not in ("queued", "running") or remaining <= 0:
                    return run
                self._condition.wait(min(remaining, 0.2))

    def _worker(self):
        try:
            while not self._stop.is_set():
                try:
                    with self._pick_lock:
                        picked = None if self._stop.is_set() else self._pick()
                    if picked:
                        run, agent, lock = picked
                        try:
                            self._execute(run, agent)
                        finally:
                            lock.release()
                            self.notify()
                        continue
                except Exception:
                    logging.exception("Fleet dispatcher iteration failed")
                with self._condition:
                    self._condition.wait(0.2)
        finally:
            with self._condition:
                self._live -= 1
                if not self._live:
                    self._instance.release()

    def _pick(self):
        with self.database.connect() as connection:
            candidates = connection.execute("""
                SELECT queued.* FROM runs queued LEFT JOIN dispatch_turns turns ON queued.agent_id = turns.agent_id
                WHERE queued.status = 'queued' AND NOT EXISTS (
                    SELECT 1 FROM runs busy WHERE busy.agent_id = queued.agent_id
                    AND busy.status IN ('running', 'needs_reconciliation'))
                AND NOT EXISTS (SELECT 1 FROM work_dependencies dep JOIN work_items upstream ON upstream.id = dep.required_id
                                WHERE dep.work_id = queued.work_id AND upstream.state != 'verified')
                ORDER BY coalesce(turns.turn, 0), queued.agent_id,
                         (queued.kind = 'tick' AND queued.decision IS NULL), queued.seq
            """).fetchall()
        locked_agents, paused_agents = set(), set()
        for candidate in candidates:
            agent_id = candidate["agent_id"]
            if agent_id in locked_agents:
                continue
            deferred_if_paused = candidate["work_id"] or (candidate["kind"] == "tick" and
                                  not candidate["decision"] and candidate["args"] == "{}")
            if deferred_if_paused and agent_id in paused_agents:
                continue
            lock = self.manager.ops_lock(agent_id)
            if not lock.acquire(blocking=False):
                locked_agents.add(agent_id)
                continue
            claimed = None
            try:
                agent = self.manager.get(agent_id)
                if agent and deferred_if_paused and agent.autonomy_view()["blocked_reason"] in ("fleet_paused", "muted"):
                    paused_agents.add(agent_id)
                    continue
                with self.database.connect(write=True) as connection:
                    run = self._get_locked(connection, candidate["id"])
                    if run["status"] != "queued":
                        continue
                    if agent is None:
                        self._save_result(connection, run, "blocked", "agent_unavailable",
                                          {"status": "blocked", "reason": "agent_unavailable"})
                        continue
                    if run["work_id"] and not self.projects.claim(connection, run):
                        continue
                    connection.execute("""
                        UPDATE runs SET status = 'running', generation = generation + 1, updated_at = ? WHERE id = ?
                    """, (time.time(), run["id"]))
                    connection.execute("UPDATE dispatch_counter SET value = value + 1")
                    connection.execute("INSERT OR REPLACE INTO dispatch_turns SELECT ?, value FROM dispatch_counter",
                                       (agent_id,))
                    next_run = self._get_locked(connection, run["id"])
                claimed = next_run
                return claimed, agent, lock
            finally:
                if claimed is None:
                    lock.release()
        return None

    def _execute(self, run, agent):
        lease = RunLease(self, run, agent)
        token = current_run.set(lease)
        try:
            result = self._invoke(run, agent, lease)
            if lease.stopped and run["kind"] in ("tick", "act") and "status" not in result:
                result = dict(result, status="cancelled", reason="run_cancelled")
            final = self.get(run["id"])
            waiting, _, _ = self._evidence(final, agent)
            with self.database.connect(write=True) as connection:
                final = self._get_locked(connection, run["id"])
                if run["kind"] == "plan" and final["cancel_requested"]:
                    result = {"status": "cancelled", "reason": "run_cancelled"}
                if run["work_id"] and result.get("status") == "completed":
                    policy = agent.autonomy_view()["blocked_reason"]
                    if policy in ("fleet_paused", "muted"):
                        result = {"status": "blocked", "reason": policy}
                status, reason = self._outcome(result, waiting, final)
                self._save_result(connection, final, status, reason, result, waiting)
        except Exception:
            logging.exception("Run interrupted: %s", run["id"])
            self._recover_run(self.get(run["id"]), agent)
        finally:
            current_run.reset(token)

    def _invoke(self, run, agent, lease):
        decision, kind, args = run["decision"], run["kind"], run["args"]
        reason = lease.reason()
        if decision and (not decision["approved"] or not reason):
            return agent.resume_task(decision["task_id"], decision["approved"])
        if reason:
            return {"status": "cancelled" if reason == "run_cancelled" else "blocked", "reason": reason}
        if kind == "task":
            return agent.run_task(args["text"])
        if kind == "plan":
            return self.task_plans.generate(run, agent, lease)
        if kind == "planned_task":
            return self.task_plans.execute(run, agent)
        if kind in ("resume", "accept", "deny"):
            prior = self.find_link(run["agent_id"], args["identifier"])
            if not prior and kind != "resume":
                proposal = next((item for item in agent.queue.pending()
                                 if item.proposal_id == args["identifier"]), None)
                prior = self.find_link(run["agent_id"], proposal.task_id) if proposal else None
            if prior and prior["id"] != run["id"]:
                return {"status": "blocked", "reason": "decision_requires_linked_run", "run": prior["id"]}
        if kind == "resume":
            return agent.resume_task(args["identifier"], args["approved"])
        if kind == "accept":
            return agent.accept_proposal(args["identifier"])
        if kind == "deny":
            return {"denied": args["identifier"], "threshold": agent.deny_proposal(args["identifier"])}
        if kind == "act":
            result = agent.force_act()
            agent._inbox.append(result.get("message") or f"行动未成：{result.get('reason', '')}")
            return result
        if kind == "tick":
            proposal = agent.maybe_proactive()
            if isinstance(proposal, str) and proposal not in agent._inbox:
                agent._inbox.append(proposal)
            if isinstance(proposal, dict):
                return {"proposal": proposal, "status": proposal.get("status"), "reason": proposal.get("reason", "")}
            return {"proposal": proposal}
        if run["work_id"]:
            return self.projects.execute(run, agent)
        raise ValueError("unknown_run_kind")

    @staticmethod
    def _outcome(result, waiting, run):
        state, reason = result.get("status"), str(result.get("reason_code") or result.get("reason") or "")
        if waiting:
            return "awaiting_approval", reason or waiting["reason"]
        if state in ("needs_reconciliation", "paused"):
            return "needs_reconciliation", reason or "unconfirmed_approval"
        if state == "cancelled" or (state == "denied" and run["cancel_requested"]):
            return "cancelled", reason or "run_cancelled"
        if (state in ("no_budget", "blocked", "no_llm") or result.get("acted") is False
                or "budget_exhausted:" in reason):
            return "blocked", reason
        if state is not None and state not in ("verified", "completed"):
            return "failed", reason or state
        return "succeeded", ""

    def _save_result(self, connection, run, status, reason, result, waiting=None):
        if run["work_id"]:
            status, reason, result = self.projects.finish(connection, run, status, reason, result)
        connection.execute("""
            UPDATE runs SET status = ?, reason = ?, result = ?, waiting = coalesce(?, waiting), updated_at = ? WHERE id = ?
        """, (status, reason, encode(result), encode(waiting) if waiting else None, time.time(), run["id"]))
        if waiting and run["cancel_requested"]:
            self._decision(connection, self._get_locked(connection, run["id"]), False, cancel=True)

    @staticmethod
    def _evidence(run, agent):
        if agent is None:
            return None, None, False
        session_id = run["session_id"]
        session, pending = None, None
        checkpoint_id = run["checkpoint_id"]
        if session_id:
            session = agent.sessions.lookup(session_id)
            if not session or not agent.sessions.compatible(session) or not agent.sessions.valid(session):
                return None, None, False
            if session["status"] in TERMINAL_STATES and session["status"] == (session.get("result") or {}).get("status"):
                return None, session["result"], True
            pending = session.get("pending_call")
            checkpoint_id = pending["task_id"] if pending else None
        if not checkpoint_id:
            return None, None, False
        checkpoint = agent.checkpoints.resume(checkpoint_id)
        if (not checkpoint or checkpoint.get("task_id") != checkpoint_id or checkpoint.get("variant_id") != VARIANT_ID
                or checkpoint.get("schema_version") != SCHEMA_VERSION or not isinstance(checkpoint.get("state"), dict)):
            return None, None, False
        state = checkpoint["state"]
        if isinstance(state.get("results"), list):
            if state.get("status") == "denied":
                return None, {"status": "denied", "task_id": checkpoint_id, "reason": "human_denied"}, True
            if state.get("status") == "completed":
                if session:
                    return {"task_id": checkpoint_id, "reason": "confirmed_checkpoint"}, None, True
                return None, {"status": "completed", "task_id": checkpoint_id, "step_results": state["results"]}, True
        proposal_id = state.get("waiting_for")
        if not proposal_id or not any(proposal.proposal_id == proposal_id and proposal.task_id == checkpoint_id
                                      for proposal in agent.queue.pending()):
            return None, None, False
        saved = state.get("pending_step")
        if pending and (not isinstance(saved, dict) or saved.get("tool") != pending["tool"]
                        or saved.get("args") != pending["args"]):
            return None, None, False
        return {"task_id": checkpoint_id, "proposal_id": proposal_id, "reason": "awaiting_approval"}, None, True

    def _recover_run(self, run, agent):
        if run["status"] != "running":
            return
        try:
            waiting, result, _ = self._evidence(run, agent)
        except Exception:
            logging.exception("Cannot read Run evidence: %s", run["id"])
            waiting, result = None, None
        if waiting:
            status, reason = "awaiting_approval", waiting["reason"]
            result = {"status": "paused", **waiting, "session_id": run["session_id"]}
        elif result is not None and run["kind"] not in ("tick", "act"):
            status, reason = self._outcome(result, None, run)
        elif run["kind"] == "plan":
            status, reason = "failed", "plan_interrupted"
            result = {"status": status, "reason": reason}
        else:
            status, reason = "needs_reconciliation", "interrupted_without_final_receipt"
            result = {"status": status, "reason": reason}
        with self.database.connect(write=True) as connection:
            latest = self._get_locked(connection, run["id"])
            if latest["status"] == "running" and latest["generation"] == run["generation"]:
                if run["kind"] == "plan" and latest["cancel_requested"]:
                    status, reason = "cancelled", "run_cancelled"
                    result = {"status": status, "reason": reason}
                self._save_result(connection, latest, status, reason, dict(result, recovered=True), waiting)

    def recover(self):
        self.budget.recover()
        with self.database.connect() as connection:
            interrupted = connection.execute("SELECT * FROM runs WHERE status = 'running' ORDER BY seq").fetchall()
        for row in interrupted:
            run = _record(row)
            try:
                agent = self.manager.get(run["agent_id"])
            except Exception:
                logging.exception("Cannot load Agent for Run recovery: %s", run["agent_id"])
                agent = None
            self._recover_run(run, agent)
