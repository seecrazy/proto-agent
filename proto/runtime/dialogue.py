"""Ordered, durable Web turns and their deferred memory inputs."""
from __future__ import annotations

import json
import re
from uuid import uuid4

from proto.coordination.coordination import CoordinationError, encode
from proto.system.execution_log import record_execution
from proto.textstyle import polish_style

RETAIN_REMEMBERED = 128
DIALOGUE_STYLE_HINT = (
    "\n用你自己的人设直接回答，先说事实、判断或下一步；不知道就明说。"
    "没有信息的开场、排比和升华不必写。别用套话替代实际共情，别为了口语化添事实。"
)
_KEEP_REPLY = re.compile(
    r"原样|一字不差|原封不动|照抄|照原文|逐字|(?:不要|不用|别)(?:改动|修改|改写|润色|换词)|"
    r"不[做作]任何(?:修改|改动)|(?:引用|复述)(?:上面|下面|如下|以下|原话|原文)|\bverbatim\b|"
    r"(?:写|创作|仿写|编)[^，。！？；\r\n]{0,10}(?:诗|歌词|剧本|台词|小说|散文|故事)|"
    r"(?:输出|返回|给出)\s*(?:纯|严格的|原始的)?\s*(?:JSON|YAML|XML|CSV|diff|一条命令)", re.I)
_FORMAL_REPLY = re.compile(r"(?:写|起草|拟定)[^，。！？；\r\n]{0,12}(?:公文|正式通告|正式公告|正式报告|正式邮件)")


def dialogue_profile(owner_text):
    if _KEEP_REPLY.search(owner_text):
        return None
    return "formal" if _FORMAL_REPLY.search(owner_text) else "dialogue"


def finish_dialogue_text(text, profile, *, execution_context):
    if profile is None:
        return text
    try:
        result = polish_style(text, profile)
    except Exception as error:
        record_execution("dialogue_style_checked", **execution_context, status="unavailable",
                         exception_type=type(error).__name__)
        return text
    before, after = result["scan_before"], result["scan_after"]
    record_execution("dialogue_style_checked", **execution_context, status=result["status"],
                     rules_version=before["rules_version"], attempt=result["attempts"],
                     hits_before=len(before["hits"]), hits_after=len(after["hits"]))
    return result["final_text"]


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
