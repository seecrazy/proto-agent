"""Durable request reservations for the shared fleet model cap."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from uuid import uuid4

from proto.kernel.budgets import parse_count
from proto.coordination.coordination import CoordinationError


@dataclass(frozen=True)
class ModelTicket:
    id: str
    cycle: str


class FleetBudget:
    def __init__(self, database, day=None):
        self.database = database
        self.day = day or (lambda: date.today().isoformat())

    @staticmethod
    def _view(connection, cycle):
        limits = dict(connection.execute("SELECT tokens, calls FROM fleet_limits").fetchone())
        usage = connection.execute("""
            SELECT coalesce(sum(tokens), 0) AS tokens, coalesce(sum(calls), 0) AS calls,
                   count(CASE WHEN state = 'uncertain' THEN 1 END) AS uncertain,
                   count(CASE WHEN state = 'reserved' THEN 1 END) AS in_flight
            FROM model_tickets WHERE cycle = ?
        """, (cycle,)).fetchone()
        return {"cycle": cycle, "caps": limits, "used": {unit: usage[unit] for unit in limits},
                "remaining": {unit: limits[unit] - usage[unit] for unit in limits},
                "uncertain": usage["uncertain"], "in_flight": usage["in_flight"]}

    def view(self):
        with self.database.connect() as connection:
            return self._view(connection, self.day())

    def for_run(self, run_id):
        with self.database.connect() as connection:
            rows = connection.execute("""
                SELECT cycle, pool, state, sum(tokens) AS tokens, sum(calls) AS calls
                FROM model_tickets WHERE run_id = ? AND state != 'released' GROUP BY cycle, pool, state
                ORDER BY cycle, pool, state
            """, (run_id,)).fetchall()
        return [dict(row) for row in rows]

    def configure(self, tokens, calls):
        tokens = parse_count(tokens, maximum=10**12)
        calls = parse_count(calls, maximum=10**12)
        if tokens is None or calls is None:
            raise CoordinationError("invalid_fleet_limits", 400)
        with self.database.connect(write=True) as connection:
            connection.execute("UPDATE fleet_limits SET tokens = ?, calls = ?", (tokens, calls))
            return self._view(connection, self.day())

    def reserve(self, agent_id, pool, tokens, run_id=None):
        if type(tokens) is not int or not 1 <= tokens <= 10**12:
            raise ValueError("invalid_token_reservation")
        if pool not in ("owner_tasks", "self_initiated", "global"):
            raise ValueError("invalid_model_pool")
        with self.database.connect(write=True) as connection:
            cycle = self.day()
            cap = self._view(connection, cycle)["remaining"]
            for unit, amount in (("tokens", tokens), ("calls", 1)):
                if cap[unit] < amount:
                    raise CoordinationError(f"fleet_budget_exhausted:{unit}")
            ticket_id = uuid4().hex
            connection.execute("""
                INSERT INTO model_tickets(id, cycle, agent_id, run_id, pool, reserved,
                                          tokens, calls, state, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'reserved', ?)
            """, (ticket_id, cycle, agent_id, run_id, pool, tokens, tokens, time.time()))
            return ModelTicket(ticket_id, cycle)

    def settle(self, ticket_id, *, tokens=None, unsent=False):
        if tokens is not None and (type(tokens) is not int or not 0 <= tokens <= 10**12 or unsent):
            raise ValueError("invalid_token_receipt")
        state = "released" if unsent else ("uncertain" if tokens is None else "settled")
        with self.database.connect(write=True) as connection:
            updated = connection.execute("""
                UPDATE model_tickets SET state = ?, tokens = coalesce(?, tokens), calls = ?
                WHERE id = ? AND state = 'reserved'
            """, (state, 0 if unsent else tokens, 0 if unsent else 1, ticket_id))
            return updated.rowcount == 1

    def recover(self):
        with self.database.connect(write=True) as connection:
            connection.execute("UPDATE model_tickets SET state = 'uncertain' WHERE state = 'reserved'")
