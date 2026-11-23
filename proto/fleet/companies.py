"""Fleet-local companies, position trees and relationship-scoped internal mail."""
from __future__ import annotations

import re
import time
from contextlib import contextmanager
from uuid import uuid4

from proto.coordination.coordination import CoordinationError


MAX_POSITIONS = 40
_TARGETS_PER_PAGE = 10
_ID = re.compile(r"[0-9a-f]{32}")
_AGENT_ID = re.compile(r"[A-Za-z0-9_-]{1,255}")
_REQUEST_KEY = re.compile(r"[A-Za-z0-9_.:-]{1,128}")

_TEMPLATES = {
    "flat": ("扁平协作", "负责人直管各岗位，岗位之间可横向协作。", (
        ("lead", None, "公司负责人", "确定方向与优先级。"),
        ("product", "lead", "产品专员", "定义需求、协调验收。"),
        ("engineering", "lead", "工程专员", "实现、测试与交付。"),
        ("operations", "lead", "运营专员", "跟踪运营效果与反馈。"),
    )),
    "functional": ("职能分层", "按产品、研发和运营分工，各专业职位向自己的职能负责人汇报。", (
        ("lead", None, "公司负责人", "确定公司方向，协调职能团队。"),
        ("product", "lead", "产品负责人", "统筹产品方向与需求。"),
        ("engineering", "lead", "研发负责人", "协调研发交付与质量。"),
        ("operations", "lead", "运营负责人", "协调运营计划与用户反馈。"),
        ("analyst", "product", "需求分析师", "分析需求并定义验收标准。"),
        ("developer", "engineering", "软件工程师", "实现并验证交付。"),
        ("qa", "engineering", "测试工程师", "检查质量与回归风险。"),
        ("operator", "operations", "运营专员", "执行运营与汇总反馈。"),
    )),
    "divisional": ("事业部制", "按业务组建独立小队，各事业部内有产品和工程。", (
        ("lead", None, "公司负责人", "制定公司目标，协调各事业部。"),
        ("alpha", "lead", "事业部 A 负责人", "推动 A 事业部的业务与交付。"),
        ("beta", "lead", "事业部 B 负责人", "推动 B 事业部的业务与交付。"),
        ("alpha-product", "alpha", "A 部产品专员", "负责 A 部需求与验收。"),
        ("alpha-engineering", "alpha", "A 部工程专员", "负责 A 部的开发和验证。"),
        ("beta-product", "beta", "B 部产品专员", "负责 B 部需求与验收。"),
        ("beta-engineering", "beta", "B 部工程专员", "负责 B 部的开发和验证。"),
    )),
    "custom": ("自定义", "只创建根职位，可自行添加职务和汇报关系。", (
        ("lead", None, "负责人", ""),
    )),
}


def _text(value, limit, field, *, optional=False, inline=False):
    error = CoordinationError(f"invalid_{field}", 400)
    if not isinstance(value, str):
        raise error
    value = value.strip()
    if (not value and not optional) or len(value) > limit or "\0" in value:
        raise error
    if inline and value and not value.isprintable():
        raise error
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise error from None
    return value


def _identifier(value, pattern, field, *, optional=False):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise CoordinationError(f"invalid_{field}", 400)
    return value


def _relation(sender, recipient):
    if sender["id"] == recipient["id"]:
        return None
    if sender["manager_id"] == recipient["id"]:
        return "report"
    if recipient["manager_id"] == sender["id"]:
        return "guidance"
    if sender["manager_id"] and sender["manager_id"] == recipient["manager_id"]:
        return "coordination"
    return None


class CompanyDirectory:
    def __init__(self, manager, database):
        self.manager = manager
        self.database = database

    @contextmanager
    def _read(self):
        with self.database.connect() as connection:
            connection.execute("BEGIN")
            yield connection

    @staticmethod
    def templates():
        return [{"id": key, "label": label, "description": description,
                 "positions": [{"id": role, "manager_id": lead, "title": title} for role, lead, title, _ in positions]}
                for key, (label, description, positions) in _TEMPLATES.items()]

    @staticmethod
    def _company(connection, company_id):
        company = connection.execute("""
            SELECT id, name, template, revision, created_at FROM companies WHERE id = ?
        """, (company_id,)).fetchone()
        if company is None:
            raise CoordinationError("no_such_company", 404)
        return dict(company)

    @staticmethod
    def _positions(connection, company_id):
        return {row["id"]: dict(row) for row in connection.execute("""
            SELECT id, company_id, manager_id, title, responsibility, agent_id FROM company_positions
            WHERE company_id = ? ORDER BY rowid
        """, (company_id,))}

    @staticmethod
    def _position(positions, position_id):
        position = positions.get(position_id)
        if position is None:
            raise CoordinationError("no_such_position", 404)
        return position

    @staticmethod
    def _require_revision(company, revision):
        if type(revision) is not int or revision < 1:
            raise CoordinationError("invalid_company_revision", 400)
        if company["revision"] != revision:
            raise CoordinationError("stale_company_revision")

    @staticmethod
    def _advance(connection, company_id, revision):
        connection.execute("UPDATE companies SET revision = revision + 1 WHERE id = ?", (company_id,))
        return revision + 1

    @staticmethod
    def _occupied(connection):
        return {row["agent_id"] for row in connection.execute(
            "SELECT agent_id FROM company_positions WHERE agent_id IS NOT NULL")}

    @staticmethod
    def _routes(position, positions, members):
        if position["agent_id"] not in members:
            return []
        targets = []
        for target in positions.values():
            if target["agent_id"] in members and (kind := _relation(position, target)):
                targets.append({"id": target["id"], "title": target["title"], "agent_id": target["agent_id"],
                                "agent_name": members[target["agent_id"]], "kind": kind})
        return targets

    def list(self):
        with self.database.connect() as connection:
            return [dict(row) for row in connection.execute("""
                SELECT id, name, template, revision FROM companies ORDER BY created_at, id
            """)]

    def create(self, name, template, *, key=None):
        name = _text(name, 80, "company_name", inline=True)
        if not isinstance(template, str) or template not in _TEMPLATES:
            raise CoordinationError("invalid_company_template", 400)
        _identifier(key, _REQUEST_KEY, "request_key", optional=True)
        with self.database.connect(write=True) as connection:
            existing = (connection.execute("SELECT id, name, template FROM companies WHERE request_key = ?",
                                           (key,)).fetchone() if key else None)
            if existing:
                if (existing["name"], existing["template"]) != (name, template):
                    raise CoordinationError("idempotency_conflict")
                return self._company(connection, existing["id"])
            company_id = uuid4().hex
            connection.execute("INSERT INTO companies (id, name, template, request_key, created_at) VALUES (?, ?, ?, ?, ?)",
                               (company_id, name, template, key, time.time()))
            ids = {}
            for role, manager_role, title, responsibility in _TEMPLATES[template][2]:
                ids[role] = uuid4().hex
                connection.execute("""
                    INSERT INTO company_positions (id, company_id, manager_id, title, responsibility)
                    VALUES (?, ?, ?, ?, ?)
                """, (ids[role], company_id, ids.get(manager_role), title, responsibility))
            return self._company(connection, company_id)

    def view(self, company_id):
        with self._read() as connection:
            company = self._company(connection, company_id)
            positions = self._positions(connection, company_id)
            members = self.manager.member_names()
            occupied = self._occupied(connection)
            messages = [dict(row) for row in connection.execute("""
                SELECT * FROM company_messages WHERE company_id = ? ORDER BY seq DESC LIMIT 30
            """, (company_id,))]
        roles = []
        for position in positions.values():
            agent_id = position["agent_id"]
            roles.append(dict(position, agent_id=agent_id if agent_id in members else None,
                              agent_name=members.get(agent_id),
                              archived_agent_id=agent_id if agent_id not in members else None,
                              routes=self._routes(position, positions, members)))
        return dict(company, positions=roles, messages=messages,
                    available_agents=[{"id": key, "name": name} for key, name in members.items() if key not in occupied])

    def save_position(self, company_id, position_id, spec):
        if set(spec) - {"title", "responsibility", "manager_id", "agent_id", "revision"} or not {
                "manager_id", "agent_id"} <= set(spec):
            raise CoordinationError("invalid_position_fields", 400)
        title = _text(spec.get("title"), 64, "position_title", inline=True)
        responsibility = _text(spec.get("responsibility", ""), 400, "position_responsibility", optional=True)
        manager_id = _identifier(spec["manager_id"], _ID, "manager_id", optional=True)
        agent_id = _identifier(spec["agent_id"], _AGENT_ID, "agent_id", optional=True)
        revision = spec.get("revision")
        with self.database.connect(write=True) as connection:
            self._require_revision(self._company(connection, company_id), revision)
            positions = self._positions(connection, company_id)
            if position_id is None and len(positions) >= MAX_POSITIONS:
                raise CoordinationError("company_position_limit")
            original = self._position(positions, position_id) if position_id is not None else None
            root = original is not None and original["manager_id"] is None
            if root and manager_id is not None:
                raise CoordinationError("company_root_locked")
            if not root and manager_id not in positions:
                raise CoordinationError("invalid_manager_id", 400)
            ancestor = manager_id
            while ancestor is not None:
                if ancestor == position_id:
                    raise CoordinationError("organization_cycle")
                ancestor = positions[ancestor]["manager_id"]
            members = self.manager.member_names()
            if agent_id is not None:
                if agent_id not in members:
                    raise CoordinationError("no_such_agent", 404)
                entry = connection.execute("SELECT id FROM company_positions WHERE agent_id = ?", (agent_id,)).fetchone()
                if entry is not None and entry["id"] != position_id:
                    raise CoordinationError("agent_already_appointed")
            elif original is not None and original["agent_id"] not in members:
                agent_id = original["agent_id"]
            if original is None:
                position_id = uuid4().hex
                connection.execute("""
                    INSERT INTO company_positions (title, responsibility, manager_id, agent_id, id, company_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (title, responsibility, manager_id, agent_id, position_id, company_id))
            else:
                connection.execute("""
                    UPDATE company_positions SET title = ?, responsibility = ?, manager_id = ?, agent_id = ? WHERE id = ?
                """, (title, responsibility, manager_id, agent_id, position_id))
            return {"id": position_id, "revision": self._advance(connection, company_id, revision)}

    def remove_position(self, company_id, position_id, revision):
        with self.database.connect(write=True) as connection:
            self._require_revision(self._company(connection, company_id), revision)
            positions = self._positions(connection, company_id)
            position = self._position(positions, position_id)
            if (position["manager_id"] is None or position["agent_id"] in self.manager.member_names()
                    or any(item["manager_id"] == position_id for item in positions.values())):
                raise CoordinationError("position_not_removable")
            connection.execute("DELETE FROM company_positions WHERE id = ?", (position_id,))
            return {"revision": self._advance(connection, company_id, revision)}

    def role(self, agent_id):
        with self.database.connect() as connection:
            row = connection.execute("""
                SELECT company.name AS company, position.id AS position_id, position.title,
                       position.responsibility, leader.title AS manager
                FROM company_positions position JOIN companies company ON company.id = position.company_id
                LEFT JOIN company_positions leader ON leader.id = position.manager_id WHERE position.agent_id = ?
            """, (agent_id,)).fetchone()
        return dict(row) if row is not None and agent_id in self.manager.member_names() else None

    @staticmethod
    def _actor(connection, agent_id, members):
        row = (connection.execute("SELECT * FROM company_positions WHERE agent_id = ?", (agent_id,)).fetchone()
               if agent_id in members else None)
        return dict(row) if row is not None else None

    def status_scope(self, agent_id):
        with self._read() as connection:
            members = self.manager.member_names()
            position = self._actor(connection, agent_id, members)
            if position is None:
                return members.keys() - self._occupied(connection) if agent_id in members else set()
            positions = self._positions(connection, position["company_id"])
            return {target["agent_id"] for target in self._routes(position, positions, members)}

    def context(self, agent_id, offset=0):
        if type(offset) is not int or not 0 <= offset < MAX_POSITIONS:
            raise CoordinationError("invalid_company_offset", 400)
        with self._read() as connection:
            members = self.manager.member_names()
            position = self._actor(connection, agent_id, members)
            if position is None:
                raise CoordinationError("no_company_position")
            company = self._company(connection, position["company_id"])
            positions = self._positions(connection, company["id"])
            routes = self._routes(position, positions, members)
        end = offset + _TARGETS_PER_PAGE
        manager = positions.get(position["manager_id"])
        return {"company": company["name"], "position_id": position["id"], "title": position["title"],
                "responsibility": position["responsibility"], "manager": manager["title"] if manager else None,
                "targets": [{key: target[key] for key in ("id", "title", "agent_name", "kind")}
                            for target in routes[offset:end]],
                "next_offset": end if len(routes) > end else None}

    def inbox(self, agent_id):
        with self._read() as connection:
            position = self._actor(connection, agent_id, self.manager.member_names())
            if position is None:
                raise CoordinationError("no_company_position")
            return [dict(row) for row in connection.execute("""
                SELECT id, from_position_id, from_title, kind, source, text, created_at
                FROM company_messages WHERE to_agent_id = ? AND to_position_id = ? ORDER BY seq DESC LIMIT 4
            """, (agent_id, position["id"]))]

    @staticmethod
    def _send(connection, sender, target, members, text, source):
        if sender["agent_id"] not in members:
            raise CoordinationError("position_vacant")
        kind = _relation(sender, target) if target is not None else None
        if kind is None:
            raise CoordinationError("unreachable_position")
        if target["agent_id"] not in members:
            raise CoordinationError("position_vacant")
        message_id = uuid4().hex
        connection.execute("""
            INSERT INTO company_messages (id, company_id, from_position_id, to_position_id,
                from_title, to_title, from_agent_id, to_agent_id, kind, source, text, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (message_id, sender["company_id"], sender["id"], target["id"], sender["title"], target["title"],
              sender["agent_id"], target["agent_id"], kind, source, text, time.time()))
        return {"id": message_id, "kind": kind}

    def send(self, agent_id, target_id, text):
        _identifier(target_id, _ID, "position_id")
        text = _text(text, 1000, "message_text")
        with self.database.connect(write=True) as connection:
            members = self.manager.member_names()
            position = self._actor(connection, agent_id, members)
            if position is None:
                raise CoordinationError("no_company_position")
            positions = self._positions(connection, position["company_id"])
            return self._send(connection, position, positions.get(target_id), members, text, "agent")

    def send_as_owner(self, company_id, spec):
        if set(spec) != {"from_position_id", "to_position_id", "text", "revision"}:
            raise CoordinationError("invalid_company_message", 400)
        source_id = _identifier(spec["from_position_id"], _ID, "position_id")
        target_id = _identifier(spec["to_position_id"], _ID, "position_id")
        text = _text(spec["text"], 1000, "message_text")
        with self.database.connect(write=True) as connection:
            self._require_revision(self._company(connection, company_id), spec["revision"])
            positions = self._positions(connection, company_id)
            sender = self._position(positions, source_id)
            return self._send(connection, sender, positions.get(target_id), self.manager.member_names(), text, "owner")
