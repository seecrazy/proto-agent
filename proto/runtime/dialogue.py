"""Ordered, durable Web turns and their deferred memory inputs."""
from __future__ import annotations

import json
import re
from uuid import uuid4

from proto.runtime.coordination import CoordinationError, encode

RETAIN_REMEMBERED = 128


class Dialogue:
    def __init__(self, database):
        self.database = database

    def reply(self, agent, text, request_key, activity):
        if request_key is not None and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_key):
            raise CoordinationError("invalid_idempotency_key", 400)
        if not agent.dialogue_lock.acquire(blocking=False):
            raise CoordinationError("chat_in_progress")
        try:
            if agent.dialogue_closed:
                raise CoordinationError("no_such_agent", 404)
            if request_key is not None:
                with self.database.connect() as connection:
                    prior = connection.execute(
                        "SELECT text, reply FROM dialogue_turns WHERE agent_id = ? AND request_key = ?",
                        (agent.fleet_id, request_key)).fetchone()
                if prior:
                    if prior["text"] != text:
                        raise CoordinationError("idempotency_conflict")
                    return prior["reply"]
            reply, facts = agent.dialogue_reply(text, activity)
            with self.database.connect(write=True) as connection:
                connection.execute("""
                    INSERT INTO dialogue_turns(agent_id, turn_id, request_key, text, reply, facts)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (agent.fleet_id, uuid4().hex, request_key, text, reply, encode(facts)))
            return reply
        finally:
            agent.dialogue_lock.release()

    def history(self, agent_id, maximum):
        with self.database.connect() as connection:
            turns = connection.execute("""
                SELECT text, reply FROM dialogue_turns WHERE agent_id = ? ORDER BY seq DESC LIMIT ?
            """, (agent_id, (maximum + 1) // 2)).fetchall()
        return [entry for row in reversed(turns) for entry in (
            {"role": "owner", "text": row["text"][:400]},
            {"role": "agent", "text": row["reply"][:400]})][-maximum:]

    def pending_agents(self):
        with self.database.connect() as connection:
            return [row[0] for row in connection.execute("""
                SELECT agent_id FROM dialogue_turns WHERE remembered = 0
                GROUP BY agent_id ORDER BY min(seq)
            """)]

    def remember_one(self, agent):
        with self.database.connect() as connection:
            row = connection.execute("""
                SELECT * FROM dialogue_turns WHERE agent_id = ? AND remembered = 0 ORDER BY seq LIMIT 1
            """, (agent.fleet_id,)).fetchone()
        if row is None:
            return False
        agent.remember_dialogue(row["text"], row["reply"], row["turn_id"], json.loads(row["facts"]))
        with self.database.connect(write=True) as connection:
            connection.execute("UPDATE dialogue_turns SET remembered = 1 WHERE seq = ?", (row["seq"],))
            connection.execute("""
                DELETE FROM dialogue_turns WHERE agent_id = ? AND remembered = 1
                AND seq < (SELECT seq FROM dialogue_turns WHERE agent_id = ? AND remembered = 1
                           ORDER BY seq DESC LIMIT 1 OFFSET ?)
            """, (agent.fleet_id, agent.fleet_id, RETAIN_REMEMBERED - 1))
        return True
