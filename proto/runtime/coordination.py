"""Local fleet transaction boundary. Agent documents keep their own stores."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path


class CoordinationError(ValueError):
    def __init__(self, reason, status=409):
        super().__init__(reason)
        self.status = status


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT NOT NULL UNIQUE, agent_id TEXT NOT NULL, kind TEXT NOT NULL,
    args TEXT NOT NULL, request_key TEXT, work_id TEXT,
    status TEXT NOT NULL DEFAULT 'queued', reason TEXT NOT NULL DEFAULT '',
    generation INTEGER NOT NULL DEFAULT 0, session_id TEXT, checkpoint_id TEXT,
    waiting TEXT, decision TEXT, result TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    UNIQUE(agent_id, request_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS run_writer ON runs(agent_id) WHERE status = 'running';
CREATE INDEX IF NOT EXISTS run_dispatch ON runs(status, agent_id, seq);
CREATE INDEX IF NOT EXISTS run_links ON runs(agent_id, session_id, checkpoint_id);
CREATE TABLE IF NOT EXISTS dispatch_turns (agent_id TEXT PRIMARY KEY, turn INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS dispatch_counter (singleton INTEGER PRIMARY KEY CHECK(singleton = 1), value INTEGER NOT NULL);
INSERT OR IGNORE INTO dispatch_counter VALUES (1, 0);
CREATE TABLE IF NOT EXISTS fleet_limits (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1), tokens INTEGER NOT NULL, calls INTEGER NOT NULL
);
INSERT OR IGNORE INTO fleet_limits VALUES (1, 200000, 2000);
CREATE TABLE IF NOT EXISTS model_tickets (
    id TEXT PRIMARY KEY, cycle TEXT NOT NULL, agent_id TEXT NOT NULL, run_id TEXT REFERENCES runs(id),
    pool TEXT NOT NULL, reserved INTEGER NOT NULL, tokens INTEGER NOT NULL, calls INTEGER NOT NULL,
    state TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS model_ticket_cycle ON model_tickets(cycle);
CREATE INDEX IF NOT EXISTS model_ticket_run ON model_tickets(run_id);
"""

_PROJECT_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, request_key TEXT UNIQUE, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), seq INTEGER NOT NULL,
    title TEXT NOT NULL, brief TEXT NOT NULL, assignee TEXT NOT NULL, reviewer TEXT, goal_ref TEXT,
    contract TEXT NOT NULL, contract_hash TEXT NOT NULL, request_key TEXT,
    state TEXT NOT NULL DEFAULT 'queued', reason TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1,
    inputs TEXT NOT NULL DEFAULT '[]', latest_version INTEGER NOT NULL DEFAULT 0,
    run_id TEXT REFERENCES runs(id), review_run_id TEXT REFERENCES runs(id),
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    UNIQUE(project_id, seq), UNIQUE(project_id, id), UNIQUE(project_id, request_key)
);
CREATE TABLE IF NOT EXISTS work_dependencies (
    project_id TEXT NOT NULL, work_id TEXT NOT NULL, required_id TEXT NOT NULL,
    PRIMARY KEY(work_id, required_id),
    FOREIGN KEY(project_id, work_id) REFERENCES work_items(project_id, id),
    FOREIGN KEY(project_id, required_id) REFERENCES work_items(project_id, id),
    CHECK (work_id != required_id)
);
CREATE TABLE IF NOT EXISTS artifact_versions (
    work_id TEXT NOT NULL REFERENCES work_items(id), version INTEGER NOT NULL,
    producer TEXT NOT NULL, contract_hash TEXT NOT NULL, snapshot TEXT NOT NULL, checks TEXT NOT NULL,
    created_at REAL NOT NULL, PRIMARY KEY(work_id, version)
);
CREATE TABLE IF NOT EXISTS artifact_reviews (
    work_id TEXT NOT NULL, version INTEGER NOT NULL, reviewer TEXT NOT NULL, accepted INTEGER NOT NULL,
    note TEXT NOT NULL, run_id TEXT NOT NULL REFERENCES runs(id), created_at REAL NOT NULL,
    PRIMARY KEY(work_id, version), FOREIGN KEY(work_id, version) REFERENCES artifact_versions(work_id, version)
);
"""

_DIALOGUE_SCHEMA = """
CREATE INDEX IF NOT EXISTS runs_agent_seq ON runs(agent_id, seq);
CREATE TABLE IF NOT EXISTS dialogue_turns (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, agent_id TEXT NOT NULL,
    turn_id TEXT NOT NULL, request_key TEXT, text TEXT NOT NULL, reply TEXT NOT NULL,
    facts TEXT NOT NULL, remembered INTEGER NOT NULL DEFAULT 0,
    UNIQUE(agent_id, request_key)
);
CREATE INDEX IF NOT EXISTS dialogue_agent ON dialogue_turns(agent_id, seq);
CREATE INDEX IF NOT EXISTS dialogue_pending ON dialogue_turns(seq) WHERE remembered = 0;
"""


class CoordinationDB:
    def __init__(self, root):
        self.directory = Path(root) / ".coordination"
        self.path = self.directory / "state.sqlite3"
        self._ready = False
        self._lock = threading.Lock()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self):
        with self._lock:
            if self._ready:
                return
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            connection = self._connect()
            try:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version not in (0, 1, 2, 3):
                    raise CoordinationError("unsupported_coordination_schema")
                if version == 0:
                    connection.execute("PRAGMA journal_mode = WAL")
                if version < 3:
                    schema = (_SCHEMA if version == 0 else "") + (_PROJECT_SCHEMA if version < 2 else "")
                    connection.executescript("BEGIN IMMEDIATE;\n" + schema + _DIALOGUE_SCHEMA
                                             + "\nPRAGMA user_version = 3; COMMIT;")
                self._ready = True
            finally:
                connection.close()

    @contextmanager
    def connect(self, *, write=False):
        self.initialize()
        connection = self._connect()
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()
