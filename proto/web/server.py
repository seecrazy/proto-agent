# -*- coding: utf-8 -*-
"""Web 控制台服务器（stdlib http.server，零依赖）。

- 仅绑定 127.0.0.1；一次性令牌置 URL 片段，API 强制 X-Proto-Token（防跨站）；
- 锁分离：只读接口（state/audit/…）无锁直读，变更操作走 mut_lock——
  长对话不再卡住界面轮询；
- chat 先回包后固化：记忆/抽取在响应发出后落盘。
"""
from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from proto.runtime.activity import activity_reply
from proto.runtime.agent import ProtoAgent
from proto.runtime.coordination import CoordinationError

INDEX_PATH = Path(__file__).with_name("index.html")

MAX_BODY_BYTES = 1_000_000   # 请求体上限；超过 → 413

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

INDEX_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
             "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
             "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")


class _HTTPError(Exception):
    """可映射为 HTTP 状态码的请求错误。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message

# 主人快通道 POST：免 agent 锁（M49 紧急控制 + M50 单资源写操作）。
# 安全性来源：kernel 旗标步边界生效；预算/目标树/审批队列各持 RLock；
# priority 只写文本（单资源），目标播种推迟到空闲心跳补做。
CONTROL_POSTS = {"/api/mute", "/api/awake", "/api/budget/refill",
                 "/api/budget/caps", "/api/priority"}
_CONTROL_POST_RE = [
    re.compile(r"^/api/goals/[0-9a-f]+/drop$"),
    re.compile(r"^/api/agents/[A-Za-z0-9_-]+/(pause|select|archive)$"),
]
_AUTONOMY_RE = re.compile(r"^/api/agents/([A-Za-z0-9_-]+)/autonomy$")
_AGENT_NAME_RE = re.compile(r"^/api/agents/([A-Za-z0-9_-]+)/name$")
_ACT_RE = re.compile(r"^/api/agents/([A-Za-z0-9_-]+)/act$")
_RESUME_RE = re.compile(r"^/api/task/([A-Za-z0-9_-]+)/resume$")
_PLAN_APPROVE_RE = re.compile(r"^/api/task/plan/([0-9a-f]{32})/approve$")
_PROPOSAL_RE = re.compile(r"^/api/proposals/([0-9a-f]+)/(accept|deny)$")
_RUN_RE = re.compile(r"^/api/runs/([0-9a-f]{32})(?:/(cancel|resume|reconcile))?$")
_PROJECT_RE = re.compile(r"^/api/projects/([0-9a-f]{32})(/work)?$")
_WORK_RE = re.compile(r"^/api/projects/([0-9a-f]{32})/work/([0-9a-f]{32})/(retry|drop|artifacts/([1-9][0-9]{0,8}))$")

_ACTIVITY_QUESTION = re.compile(
    r"(?:你)?(?:现在|目前)?在(?:干嘛|干吗|干什么|干啥|做什么|做啥|忙什么)[呢啊呀]?[?？]*"
)
_PAST_ACTIVITY_QUESTION = re.compile(
    r"(?:你)?(?:刚才|刚刚|最近|上一轮)(?:都)?"
    r"(?:做了什么|做了啥|干了什么|完成了什么|产出了什么|有什么产出)[呢啊呀]?[?？]*"
)

READ_ONLY_GETS = {
    "/api/state", "/api/audit", "/api/proposals", "/api/rules",
    "/api/evolution", "/api/scratch", "/api/memory", "/api/inbox",
    "/api/goaltree", "/api/world", "/api/taste", "/api/agents",
    "/api/summary", "/api/blackboard", "/api/projects-root", "/api/methods",
    "/api/identity", "/api/activity", "/api/activity/live",
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "ProtoWeb/0.33.0"

    def log_message(self, fmt, *args):
        pass

    # ---- 基础设施 ----
    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Proto-Token") or ""
        return hmac.compare_digest(supplied, self.server.token)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        if length > MAX_BODY_BYTES:
            # 有界丢弃请求体：直接回 413 不读会 RST 客户端写管道
            remaining = min(length, MAX_BODY_BYTES * 10)
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
            raise _HTTPError(413, "body_too_large")
        try:
            parsed = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise _HTTPError(400, "invalid_json")
        if not isinstance(parsed, dict):
            raise _HTTPError(400, "invalid_json_body")
        return parsed

    def _safely(self, fn):
        try:
            return fn()
        except _HTTPError as exc:
            return self._json(exc.status, {"error": exc.message})
        except CoordinationError as exc:
            return self._json(exc.status, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            return None
        except Exception:
            logging.exception("API 路由异常")
            return self._json(500, {"error": "internal"})

    # ---- HTTP ----
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            data = INDEX_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", INDEX_CSP)
            for key, value in SECURITY_HEADERS.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if parsed.path.startswith("/api/"):
            if not self._authorized():
                return self._json(401, {"error": "unauthorized"})
            return self._safely(lambda: self._api("GET", parsed))
        self._json(404, {"error": "not_found"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            return self._json(404, {"error": "not_found"})
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})
        return self._safely(lambda: self._api("POST", parsed))

    # ---- 路由入口（锁分离） ----
    def _api(self, method: str, parsed):
        server: ProtoWebServer = self.server
        path, query = parsed.path, parse_qs(parsed.query)
        if path in {"/api/models", "/api/models/select", "/api/models/reload"}:
            return self._models(method, path, server)
        autonomy = _AUTONOMY_RE.fullmatch(path)
        if autonomy:
            return self._autonomy(method, server.manager, autonomy.group(1))
        if method == "POST" and path in ("/api/agents/preview", "/api/agents"):
            return self._agent_creation(path, server.manager)
        rename = _AGENT_NAME_RE.fullmatch(path)
        if method == "POST" and rename:
            return self._agent_rename(server.manager, rename.group(1))
        if path == "/api/projects" or path.startswith("/api/projects/"):
            return self._project_routes(method, path, server.manager.runs.projects)
        agent_id = self.headers.get("X-Proto-Agent") or server.manager.current_id()
        if (path in ("/api/runs", "/api/fleet-budget") or path.startswith(("/api/jobs/", "/api/runs/"))
                or (method == "POST" and (path in ("/api/task", "/api/task/plan", "/api/tick")
                    or any(pattern.fullmatch(path) for pattern in (_ACT_RE, _RESUME_RE, _PROPOSAL_RE, _PLAN_APPROVE_RE))))):
            return self._run_routes(method, path, query, agent_id, server.manager.runs)
        agent = server.manager.get(agent_id)
        if method == "POST" and path == "/api/chat":
            if agent is None:
                raise _HTTPError(404, "no_such_agent")
            chat_text = self._body().get("text")
            if not isinstance(chat_text, str) or not chat_text.strip() or len(chat_text) > 16000:
                raise _HTTPError(400, "invalid_chat_text")
            try:
                chat_text.encode("utf-8")
            except UnicodeError:
                raise _HTTPError(400, "invalid_chat_text") from None
            question = chat_text.strip()
            past_activity = _PAST_ACTIVITY_QUESTION.fullmatch(question) is not None
            if past_activity or _ACTIVITY_QUESTION.fullmatch(question):
                reply = activity_reply(server.manager.runs.activity.snapshot(agent), detailed=past_activity)
                return self._json(200, {"reply": reply, "read_only": True})
            return self._chat(chat_text, agent, server.manager.runs)
        if method == "POST" and path == "/api/judge":
            return self._safely(lambda: self._routes(method, path, query,
                                                    agent, server))
        # M49/M50 主人快通道：免锁直通（安全性见 CONTROL_POSTS 注释）
        if method == "POST" and (path in CONTROL_POSTS
                                 or any(rx.match(path)
                                        for rx in _CONTROL_POST_RE)):
            return self._routes(method, path, query, agent, server)
        if method == "GET" and path in READ_ONLY_GETS:
            return self._routes(method, path, query, agent, server)
        # M50：per-agent 单写者锁——A 的长链只挡 A 的操作，不再挡全舰队
        lock = (server.manager.ops_lock(agent_id)
                if agent is not None else server.mut_lock)
        if not lock.acquire(timeout=3.0):
            return self._json(409, {"error": "agent_busy",
                                    "hint": "该 agent 自学习或长任务进行中，"
                                            "稍后重试（不影响其他 agent）"})
        try:
            if agent is None:
                return self._routes(method, path, query, agent, server)
            with agent.background_write():
                return self._routes(method, path, query, agent, server)
        finally:
            lock.release()

    def _chat(self, text, agent, runs):
        reply = runs.dialogue.reply(
            agent, text, self.headers.get("Idempotency-Key"),
            lambda: activity_reply(runs.activity.snapshot(agent), detailed=True))
        runs.notify()
        return self._json(200, {"reply": reply})

    def _run_routes(self, method, path, query, agent_id, runs):
        if path == "/api/fleet-budget":
            if method == "GET":
                return self._json(200, runs.budget.view())
            body = self._body()
            return self._json(200, runs.budget.configure(body.get("tokens"), body.get("calls")))
        key = self.headers.get("Idempotency-Key")
        if path == "/api/runs":
            if method == "GET":
                try:
                    before = int(query["before"][0]) if "before" in query else None
                    if before is not None and not 0 < before < 2**63:
                        raise ValueError
                except ValueError:
                    raise _HTTPError(400, "invalid_run_cursor")
                return self._json(200, {"runs": runs.list(query.get("agent", [None])[0], before=before,
                                                         active=query.get("active") == ["true"],
                                                         reviewable=query.get("reviewable") == ["true"])})
            body = self._body()
            kind = body.get("kind")
            if kind not in ("task", "act", "tick"):
                raise _HTTPError(400, "invalid_run_kind")
            target_id = body.get("agent_id", agent_id)
            if not isinstance(target_id, str):
                raise _HTTPError(400, "agent_id_required")
            args = (self._task_args(body) if kind == "task" else {"explicit": True} if kind == "tick" else {})
            return self._json(202, runs.enqueue(target_id, kind, args, key=key))
        match = _RUN_RE.fullmatch(path)
        if match:
            run_id, action = match.groups()
            if method == "GET" and not action:
                run = runs.get(run_id)
                return self._json(200, dict(run, budget=runs.budget.for_run(run_id),
                                            blocked_reason=runs.blocked_reason(run)))
            if method == "POST" and action:
                body = self._body()
                if action == "cancel":
                    return self._json(200, runs.cancel(run_id))
                if action == "resume":
                    return self._json(202, runs.decide(run_id, body.get("approved"), body.get("generation")))
                if body.get("confirmed") is not True:
                    raise _HTTPError(400, "reconciliation_confirmation_required")
                if body.get("continue_safe") is True:
                    return self._json(202, runs.continue_reconciliation(run_id, body.get("generation")))
                if "continue_safe" in body and body["continue_safe"] is not False:
                    raise _HTTPError(400, "invalid_reconciliation_action")
                return self._json(200, runs.close_reconciliation(run_id, body.get("generation")))
        job = re.fullmatch(r"/api/jobs/([0-9a-f]+)", path)
        if method == "GET" and job:
            try:
                run = runs.get(job.group(1))
            except CoordinationError as exc:
                raise _HTTPError(exc.status, "no_such_job") from exc
            status = "running" if run["status"] in ("queued", "running") else "done"
            if run["status"] in ("failed", "needs_reconciliation"):
                status = "error"
            return self._json(200, {"status": status, "result": run["result"], "agent": run["agent_id"],
                                    "created_at": run["created_at"],
                                    "done_at": run["updated_at"] if status != "running" else None})
        if method != "POST":
            raise _HTTPError(404, "not_found")
        act = _ACT_RE.fullmatch(path)
        if act:
            run = runs.enqueue(act.group(1), "act", key=key)
            return self._json(200, {"job": run["id"], "status": "running"})
        if path == "/api/tick":
            return self._wait_run(runs, runs.enqueue(agent_id, "tick", {"explicit": True}, key=key))
        if path == "/api/task":
            body = self._body()
            run = runs.enqueue(agent_id, "task", self._task_args(body), key=key)
            return self._wait_run(runs, run, asynchronous=body.get("async") is True)
        if path == "/api/task/plan":
            body = self._body()
            run = runs.task_plans.enqueue(agent_id, body.get("text"), body.get("project_dir", ""),
                                          body.get("outputs", []), key=key)
            return self._wait_run(runs, run, asynchronous=True)
        approval = _PLAN_APPROVE_RE.fullmatch(path)
        if approval:
            run = runs.task_plans.approve(approval.group(1), self._body().get("sha256"))
            return self._wait_run(runs, run, asynchronous=True)
        resume, proposal = _RESUME_RE.fullmatch(path), _PROPOSAL_RE.fullmatch(path)
        if resume or proposal:
            body = self._body()
            identifier = (resume or proposal).group(1)
            kind = "resume" if resume else proposal.group(2)
            approved = body.get("approved") if resume else kind == "accept"
            if type(approved) is not bool:
                raise _HTTPError(400, "approved_must_be_boolean")
            run = runs.find_link(agent_id, identifier)
            if run:
                if run["status"] == "awaiting_approval":
                    run = runs.decide(run["id"], approved, run["generation"])
                elif not run["decision"] or run["decision"]["approved"] is not approved:
                    raise _HTTPError(409, "run_not_waiting")
            else:
                args = {"identifier": identifier, "approved": approved}
                run = runs.enqueue(agent_id, kind, args, key=key)
            return self._wait_run(runs, run, asynchronous=body.get("async") is True)
        raise _HTTPError(404, "not_found")

    @staticmethod
    def _task_args(body):
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 16000:
            raise _HTTPError(400, "invalid_task_text")
        return {"text": text}

    def _wait_run(self, runs, run, *, asynchronous=False):
        if not asynchronous:
            run = runs.wait(run["id"])
            if run["status"] not in ("queued", "running") and run["result"] is not None:
                return self._json(200, run["result"])
        return self._json(202, {"run": run["id"], "status": run["status"]})

    def _project_routes(self, method, path, projects):
        key = self.headers.get("Idempotency-Key")
        if path == "/api/projects":
            if method == "GET":
                return self._json(200, {"projects": projects.list()})
            body = self._body()
            return self._json(201, projects.create(body.get("name"), body.get("description", ""), key=key))
        project = _PROJECT_RE.fullmatch(path)
        if project:
            project_id, work = project.groups()
            if method == "GET" and not work:
                return self._json(200, projects.view(project_id))
            if method == "POST" and work:
                return self._json(202, projects.add_work(project_id, self._body(), key=key))
        item = _WORK_RE.fullmatch(path)
        if item:
            project_id, work_id, action, version = item.groups()
            if method == "GET" and version:
                return self._json(200, projects.artifact(project_id, work_id, int(version)))
            if method == "POST" and not version:
                return self._json(200, projects.change(project_id, work_id, self._body().get("revision"), action))
        raise _HTTPError(404, "not_found")

    def _agent_creation(self, path, manager):
        fields = self._body()
        if "name" not in fields or set(fields) - {"name", "persona", "priority", "mode"}:
            raise _HTTPError(400, "invalid_agent_fields")
        try:
            if path.endswith("/preview"):
                return self._json(200, {"preview": manager.preview(**fields)})
            info = manager.create(**fields)
        except ValueError as exc:
            raise _HTTPError(400, str(exc)) from exc
        manager.set_current(info["id"])
        return self._json(200, {"created": info, "agents": manager.list()})

    def _agent_rename(self, manager, agent_id):
        fields = self._body()
        if set(fields) != {"name"}:
            raise _HTTPError(400, "invalid_agent_fields")
        try:
            info = manager.rename(agent_id, fields["name"])
        except ValueError as exc:
            raise _HTTPError(400, str(exc)) from exc
        if info is None:
            raise _HTTPError(404, "no_such_agent")
        return self._json(200, info)

    def _autonomy(self, method, manager, agent_id):
        from proto.runtime.autonomy import AutonomyConflict
        info = manager.info(agent_id)
        agent = manager.get(agent_id)
        if info is None or agent is None:
            raise _HTTPError(404, "no_such_agent")
        if method == "POST":
            fields = self._body()
            if (set(fields) - {"enabled", "mode", "expected_revision"}
                    or not {"enabled", "mode"}.intersection(fields)
                    or any(value is None for value in fields.values())):
                raise _HTTPError(400, "invalid_autonomy_fields")
            try:
                view = agent.configure_autonomy(**fields)
            except AutonomyConflict as exc:
                raise _HTTPError(409, str(exc)) from exc
            except ValueError as exc:
                raise _HTTPError(400, str(exc)) from exc
        else:
            view = agent.autonomy_view()
        return self._json(200, {"autonomy": view, "name": info["name"],
                                "scope": {"projects_root": agent.projects_root(),
                                          "budgets": _budget_view(agent),
                                          "tools": sorted(agent.tools.names())}})

    def _models(self, method, path, server):
        from proto.llm.catalog import ModelConfigError, ModelRevisionConflict
        catalog = server.manager.models
        if method == "GET" and path == "/api/models":
            return self._json(200, catalog.view())
        if method != "POST":
            return self._json(405, {"error": "method_not_allowed"})
        body = self._body()
        expected = body.get("expected_revision")
        try:
            if path == "/api/models":
                if set(body) != {"expected_revision", "profile"}:
                    raise ModelConfigError("invalid_model_request")
                result = catalog.save_profile(body["profile"], expected)
            elif path == "/api/models/select":
                if set(body) != {"expected_revision", "main", "fast"}:
                    raise ModelConfigError("invalid_model_request")
                result = catalog.select(body["main"], body["fast"], expected)
            else:
                if set(body) != {"expected_revision"}:
                    raise ModelConfigError("invalid_model_request")
                result = catalog.reload(expected)
        except ModelRevisionConflict as exc:
            return self._json(409, {"error": str(exc)})
        except ModelConfigError as exc:
            return self._json(400, {"error": str(exc)})
        return self._json(200, result)

    def _routes(self, method: str, path: str, query, agent: ProtoAgent,
                server: ProtoWebServer):
        if path == "/api/agents" and method == "GET":
            return self._json(200, {"agents": server.manager.list(),
                                    "current": server.manager.current_id()})
        m4 = re.match(r"^/api/agents/([A-Za-z0-9_-]+)/(select|pause|archive)$", path)
        if m4 and method == "POST":
            aid, action = m4.group(1), m4.group(2)
            if action == "select":
                server.manager.set_current(aid)
            elif action == "pause":
                on = self._body().get("on", True)
                if type(on) is not bool:
                    raise _HTTPError(400, "on_must_be_boolean")
                if not server.manager.pause(aid, on):
                    raise _HTTPError(404, "no_such_agent")
            elif action == "archive":
                if not server.manager.archive(aid):
                    return self._json(404, {"error": "no_such_agent"})
            return self._json(200, {"agents": server.manager.list(),
                                    "current": server.manager.current_id()})

        if path == "/api/state" and method == "GET":
            return self._json(200, _state_payload(agent, server))

        if path == "/api/proposals" and method == "GET":
            return self._json(200, {"proposals": agent.pending_proposals()})

        if path == "/api/audit" and method == "GET":
            n = int(query.get("n", ["20"])[0])
            return self._json(200, {
                "events": agent.audit_tail(n),
                "chain_ok": agent.spine.verify_chain().ok,
            })

        if path == "/api/budget/caps" and method == "POST":
            # M52：上限直改（支持 8k/16万/1.2M/裸数字），立即生效并持久化
            from proto.kernel.budgets import parse_amount
            body = self._body()
            pools = agent.kernel.pools
            applied = {}
            fields = {"global_tokens": ("global", "tokens"),
                      "owner_tasks_tokens": ("owner_tasks", "tokens"),
                      "self_initiated_tokens": ("self_initiated", "tokens"),
                      "global_calls": ("global", "calls")}
            for key, val in body.items():
                if key not in fields:
                    return self._json(400, {"error": "bad_budget_field", "field": key})
                amount = parse_amount(val)
                if amount is None:
                    return self._json(400, {"error": "bad_amount",
                                            "hint": f"{key}={val!r} 无法解析"
                                                    "（支持 8k/16万/1.2M/数字）"})
                pool, unit = fields[key]
                applied.setdefault(pool, {})[unit] = amount
            if applied:
                pools.set_caps(applied)
                from proto.kernel.audit import AuditEvent
                agent.spine.append(AuditEvent.OWNER_OVERRIDE,
                                   {"action": "budget_caps", "caps": applied})
            return self._json(200, {"caps": applied,
                                    "remaining": pools.remaining_all()})
        if path == "/api/budget/refill" and method == "POST":
            agent.kernel.pools.refill()
            remaining = agent.kernel.pools.remaining_all()
            from proto.kernel.audit import AuditEvent
            agent.spine.append(AuditEvent.OWNER_OVERRIDE,
                               {"action": "budget_refill"})
            return self._json(200, {"refilled": True, "remaining": remaining})
        if path == "/api/mute" and method == "POST":
            on = self._body().get("on")
            if type(on) is not bool:
                raise _HTTPError(400, "on_must_be_boolean")
            agent.kernel.set_mute(on)
            return self._json(200, {"muted": agent.kernel.muted})

        if path == "/api/lock" and method == "POST":
            body = self._body()
            return self._json(200, agent.lock_param(body.get("param", ""),
                                                    bool(body.get("on"))))
        if path == "/api/override" and method == "POST":
            body = self._body()
            return self._json(200, agent.owner_set(body.get("param", ""),
                                                   float(body.get("value", 0))))

        if path == "/api/priority" and method == "POST":
            # M50 快通道：文本即存；目标播种由空闲心跳补做（seed_deferred）
            try:
                result = agent.set_priority_text(self._body().get("text", ""))
            except ValueError as exc:
                raise _HTTPError(400, str(exc)) from exc
            return self._json(200, result)
        if path == "/api/projects-root" and method == "GET":
            return self._json(200, {"root": agent.projects_root()})
        if path == "/api/projects-root" and method == "POST":
            try:
                root = agent.set_projects_root(self._body().get("path", ""))
            except ValueError as exc:
                return self._json(400, {"error": str(exc)})
            return self._json(200, {"root": root})
        if path == "/api/taste" and method == "GET":
            from proto.runtime.taste import load_taste
            return self._json(200, {"taste": load_taste(agent.spine.store)})
        if path == "/api/taste" and method == "POST":
            from proto.runtime.taste import refine_from_feedback
            merged = refine_from_feedback(agent,
                                          self._body().get("feedback", ""))
            if merged is None:
                return self._json(200, {"taste":
                                        __import__("proto.runtime.taste",
                                                   fromlist=["load_taste"])
                                        .load_taste(agent.spine.store),
                                        "refined": False})
            return self._json(200, {"taste": merged, "refined": True})
        if path == "/api/judge" and method == "POST":
            from proto.runtime.taste import judge, weighted_score
            verdicts = judge(agent, self._body().get("subject", ""))
            if verdicts is None:
                return self._json(200, {"verdicts": [], "score": None})
            return self._json(200, {"verdicts": verdicts,
                                    "score": weighted_score(verdicts)})
        if path == "/api/persona" and method == "POST":
            try:
                profile = agent.set_persona(self._body().get("text"))
            except ValueError as exc:
                raise _HTTPError(400, str(exc)) from exc
            return self._json(200, {"persona": agent.persona, "profile": profile})

        if path == "/api/awake" and method == "POST":
            on = bool(self._body().get("on"))
            keeper = server.keeper
            if on:
                keeper.start()
            else:
                keeper.stop()
            agent.spine.append("owner_override", {"action": "keep_awake", "on": on})
            return self._json(200, {"awake": keeper.is_active,
                                    "available": keeper.available})

        if path == "/api/goaltree" and method == "GET":
            return self._json(200, agent.goal_tree.snapshot())
        m6 = re.match(r"^/api/goals/([0-9a-f]+)/drop$", path)
        if m6 and method == "POST":
            agent.drop_goal(m6.group(1))
            return self._json(200, agent.goal_tree.snapshot())
        delivery = re.fullmatch(r"/api/goals/([0-9a-f]{12})/retry", path)
        if delivery and method == "POST":
            agent.retry_delivery(delivery.group(1))
            return self._json(200, {"goal_id": delivery.group(1), "ready": True})
        if path == "/api/world" and method == "GET":
            return self._json(200, agent.world.snapshot())
        if path == "/api/memory" and method == "GET":
            return self._json(200, agent.memory.snapshot())
        if path == "/api/memory/forget" and method == "POST":
            agent.forget_memory(self._body().get("id", ""))
            return self._json(200, agent.memory.snapshot())

        if path == "/api/summary" and method == "GET":
            from proto.runtime.summaries import bucket_events
            events = agent.audit_tail(500)
            return self._json(200, {"buckets": bucket_events(events)})
        if path == "/api/identity" and method == "GET":
            return self._json(200, {
                "ocean": agent.identity.ocean,
                "seed": agent.identity.seed,
                "values": agent.identity.values,
                "speech_style": agent.identity.speech_style,
                "prompt_block": agent.identity.prompt_block(),
                "tension": round(agent.tension(), 3),
                "drive_modulators": agent.identity.drive_modulators(),
            })
        if path == "/api/methods" and method == "GET":
            snap = agent.methods_lib.snapshot()
            snap["menu"] = [m.name for m in
                            agent.methods_lib.menu(agent.persona,
                                                    agent.state.owner_priority or "")]
            return self._json(200, snap)
        if path == "/api/activity" and method == "GET":
            return self._json(200, _activity_view(agent))
        if path == "/api/activity/live" and method == "GET":
            if agent is None:
                raise _HTTPError(404, "no_such_agent")
            return self._json(200, server.manager.runs.activity.snapshot(agent))
        if path == "/api/blackboard" and method == "GET":
            return self._json(200, {"notes": agent.read_blackboard()})
        if path == "/api/blackboard" and method == "POST":
            nid = agent.post_to_blackboard(self._body().get("text", ""))
            return self._json(200, {"id": nid})
        if path == "/api/quota" and method == "POST":
            import os
            n = int(self._body().get("n", 0))
            os.environ["PROTO_AUTO_APPROVE_N"] = str(n)
            from proto.runtime.quota import AutoApproveQuota
            agent.auto_quota = AutoApproveQuota(n) if n > 0 else None
            return self._json(200, {"auto_approve_n": n})
        if path == "/api/inbox" and method == "GET":
            return self._json(200, {"messages": agent.pop_inbox()})

        if path == "/api/scratch" and method == "GET":
            notes = []
            for rel in agent.workspace.list_scratch():
                if not rel.startswith("notes/"):
                    continue
                note_path = agent.workspace._scratch / rel
                try:
                    content = note_path.read_text(encoding="utf-8")
                    mtime = note_path.stat().st_mtime
                except OSError:
                    continue
                notes.append({"path": rel, "preview": content[:160],
                              "mtime": mtime})
            return self._json(200, {"notes": notes[-10:]})

        if path == "/api/rules" and method == "GET":
            return self._json(200, {"rules": agent.rules_view()})
        if path == "/api/rules" and method == "POST":
            rid = agent.instruct(self._body().get("text", ""))
            return self._json(200, {"rule_id": rid, "rules": agent.rules_view()})
        m2 = re.match(r"^/api/rules/([0-9a-f]+)/(deinternalize|edit|delete)$", path)
        if m2 and method == "POST" and m2.group(2) == "edit":
            try:
                agent.edit_rule(m2.group(1), self._body().get("text", ""))
            except KeyError:
                return self._json(404, {"error": "no_such_rule"})
            return self._json(200, {"rules": agent.rules_view()})
        if m2 and method == "POST" and m2.group(2) == "delete":
            try:
                agent.delete_rule(m2.group(1))
            except KeyError:
                return self._json(404, {"error": "no_such_rule"})
            return self._json(200, {"rules": agent.rules_view()})
        m2 = re.match(r"^/api/rules/([0-9a-f]+)/deinternalize$", path)
        if m2 and method == "POST":
            agent.deinternalize_rule(m2.group(1))
            return self._json(200, {"rules": agent.rules_view()})

        if path == "/api/evolution" and method == "GET":
            return self._json(200, agent.evolution_snapshot())
        if path == "/api/evolution/testset" and method == "POST":
            body = self._body()
            cid = agent.add_test_case(body.get("input"), body.get("expected"))
            return self._json(200, {
                "case_id": cid,
                "testset_count": agent.evolution_snapshot()["testset_count"]})
        if path == "/api/evolution/mutate" and method == "POST":
            return self._json(200, agent.evolve(self._body().get("code", "")))
        if path == "/api/evolution/promote" and method == "POST":
            body = self._body()
            ok = agent.promote_variant(body.get("variant_id", ""),
                                       body.get("target_class", "prompt_template"))
            return self._json(200, {"promoted": ok})

        return self._json(404, {"error": "not_found"})


def _activity_view(agent: ProtoAgent) -> dict:
    """M51 活动中心：现在在干嘛（链实时标记）+ 每目标接力日志 + 近 24h 落盘。"""
    import time as _t
    from pathlib import Path as _P
    from proto.work.delivery import project_files, stage_waiters
    from proto.work.journal import GoalJournal
    cur = getattr(agent, "_current_op", None)
    if cur:
        cur = dict(cur, elapsed_s=round(_t.time() - cur.get("since", _t.time())))
    goals = []
    active = agent.goal_tree.all_active()
    node_text = {n.id: n.text for n in active}
    prerequisites = stage_waiters(agent.goal_tree, active)
    work_dir = _P(agent.data_dir) / "work"
    goal_ids = set(node_text)
    if work_dir.is_dir():
        goal_ids.update(path.name for path in work_dir.iterdir() if path.is_dir())
    for goal_id in sorted(goal_ids):
        j = GoalJournal(work_dir / goal_id / "JOURNAL.md")
        lr = j.latest_round()
        delivery = agent.delivery_state(goal_id) if goal_id in node_text else {}
        if lr or delivery.get("blocked") or goal_id in prerequisites:
            goals.append({"goal_id": goal_id, "text": (node_text.get(goal_id) or "（目标已归档）")[:60],
                          "delivery": delivery, "waiting_for_stage": goal_id in prerequisites,
                          **j.activity_view(), **(lr or {})})
    goals.sort(key=lambda g: (not g["delivery"].get("blocked"), -g["rounds"]))
    files = []
    outputs = {}
    proot = agent.code_workspace.root / "projects"
    cutoff = _t.time() - 86400
    if proot.is_dir():
        for f in project_files(proot):
            try:
                relative = f.relative_to(agent.code_workspace.root)
                if f.name.startswith("_"):
                    continue
                st = f.stat()
            except OSError:
                continue
            if len(relative.parts) > 2:
                directory = relative.parts[1]
                row = outputs.setdefault(directory, {"dir": directory, "files": 0, "last_write": 0})
                row["files"] += 1
                row["last_write"] = max(row["last_write"], round(st.st_mtime))
            if st.st_mtime >= cutoff:
                files.append({"path": str(relative),
                              "mtime": round(st.st_mtime), "size": st.st_size})
        files.sort(key=lambda x: -x["mtime"])
    evs = []
    for e in agent.audit_tail(20):
        pe = e.get("payload", {})
        t = e.get("event_type", "")
        if t == "tool_exec":
            detail = str(pe.get("path") or pe.get("tool") or "")[:80]
        else:
            detail = str(pe.get("goal") or pe.get("action")
                         or pe.get("event") or "")[:60]
        evs.append({"ts": e.get("ts"), "type": t, "detail": detail})
    return {"current": cur, "goal_work": goals[:6],
            "recent_files": files[:12], "recent_events": evs[:15],
            "project_outputs": sorted(outputs.values(), key=lambda row: -row["last_write"])[:8],
            # M59/M61：空闲成因按意义门控暴露；冷却字段已废除，避免旧文案复生。
            "last_act_ts": getattr(agent, "_last_self_act", 0.0),
            "idle_policy": "meaning_gate",
            "has_goals": bool(goal_active := agent.goal_tree.all_active())}


def _budget_view(agent: ProtoAgent) -> dict:
    """余量 + 上限（M44：cap_* 前缀，前端画占比条）。"""
    try:
        view = {}
        for name, units in agent.kernel.pools._remaining.items():
            row = dict(units)
            for unit, cap in agent.kernel.pools._config.get(name, {}).items():
                row[f"cap_{unit}"] = cap
            view[name] = row
        return view
    except AttributeError:
        return {}


def _state_payload(agent: ProtoAgent, server: ProtoWebServer = None) -> dict:
    params = {}
    for cat in ("instincts", "desires", "personality"):
        rows = []
        for name, spec in getattr(agent.state, cat).items():
            row = {"name": name, "value": spec["value"], "setpoint": spec["setpoint"],
                   "bounds": spec["bounds"],
                   "owner_locked": bool(spec.get("owner_locked")),
                   "dormant": bool(spec.get("dormant"))}
            if "origin" in spec:
                row["origin"] = spec["origin"]
            if "threshold" in spec:
                row["threshold"] = spec["threshold"]
            rows.append(row)
        params[cat] = rows
    last = agent.state.modulation_log[-1] if agent.state.modulation_log else None
    return {
        "params": params,
        "emotion": dict(agent.state.emotion),
        "owner_priority": agent.state.owner_priority,
        "persona": agent.persona,
        "profile": agent.profile.to_dict(),
        "autonomy": agent.autonomy_view(),
        "budgets": _budget_view(agent),
        "model": (agent.llm.describe() if hasattr(agent.llm, "describe")
                  else "已接 LLM") if agent.llm else "离线模式",
        "muted": agent.kernel.muted,
        "chain_ok": agent.spine.verify_chain().ok,
        "awake": bool(server and server.keeper.is_active),
        "awake_available": bool(server and server.keeper.available),
        "last_modulation": last,
    }


class ProtoWebServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, agent: ProtoAgent = None, host: str = "127.0.0.1",
                 port: int = 8420, token: str = None, heartbeat_s: float = 30.0,
                 keep_awake=None, manager=None, workers: int = 2):
        from proto.runtime.manager import AgentManager
        if manager is None:
            assert agent is not None, "需要 agent 或 manager 之一"
            # 单 agent 收编：registry 放 agent 自家 .fleet/，与兄弟实例隔离
            # （root=parent 会让同父目录的多实例共享 registry、互相劫持 current）
            manager = AgentManager(root=agent.data_dir / ".fleet", llm=agent.llm,
                                   fast_llm=agent.llm_fast,
                                   webtools=agent.webtools)
            manager.adopt(agent)
        self.manager = manager
        manager.enable_model_updates()
        self.agent = agent or manager.get(manager.current_id())  # 遗留引用
        self.token = token or secrets.token_urlsafe(16)
        from proto.system.keepawake import KeepAwake
        self.keeper = keep_awake if keep_awake is not None else KeepAwake()
        self.lock = threading.Lock()      # 遗留别名（心跳）
        self.mut_lock = threading.Lock()  # 变更长锁：chat/task/evolve
        self._hb_stop = threading.Event()
        super().__init__((host, port), _Handler)
        try:
            manager.runs.start(workers)
        except BaseException:
            super().server_close()
            raise
        if heartbeat_s > 0:
            threading.Thread(target=self._heartbeat, args=(heartbeat_s,),
                             daemon=True, name="proto-heartbeat").start()

    @staticmethod
    def _effective_interval(interval: float, hour: int) -> float:
        """昼夜节律节流：默认 8-23 点为活跃期，夜间间隔×5——凌晨的 PROTO 也在睡觉。"""
        return interval if 8 <= hour < 23 else interval * 5

    def _heartbeat(self, interval: float) -> None:
        import time as _time
        while not self._hb_stop.wait(
                self._effective_interval(interval, _time.localtime().tm_hour)):
            try:
                self.manager.enqueue_ticks()
            except Exception:
                logging.exception("Fleet heartbeat enqueue failed")

    def server_close(self) -> None:
        self._hb_stop.set()
        self.manager.runs.stop()
        super().server_close()

    @property
    def port(self) -> int:
        return self.server_address[1]

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/#token={self.token}"
