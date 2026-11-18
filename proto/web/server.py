# -*- coding: utf-8 -*-
"""Web 控制台服务器（stdlib http.server，零依赖）。

- 仅绑定 127.0.0.1；一次性令牌置 URL 片段，API 强制 X-Proto-Token（防跨站）；
- 锁分离：只读接口（state/audit/…）无锁直读，变更操作走 mut_lock——
  长对话不再卡住界面轮询；
- chat 先回包后固化：主人零等待，记忆/抽取在响应发出后落盘。
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

from proto.runtime.agent import ProtoAgent

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

READ_ONLY_GETS = {
    "/api/state", "/api/audit", "/api/proposals", "/api/rules",
    "/api/evolution", "/api/scratch", "/api/memory", "/api/inbox",
    "/api/goaltree", "/api/world", "/api/taste", "/api/agents",
    "/api/summary", "/api/blackboard", "/api/projects-root",
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "ProtoWeb/0.30.0"

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
        agent = server.manager.agent_for_request(
            self.headers.get("X-Proto-Agent"))
        path, query = parsed.path, parse_qs(parsed.query)
        if method == "GET" and path.startswith("/api/jobs/"):
            return self._safely(lambda: self._routes(method, path, query,
                                                    agent, server))
        if method == "POST" and path == "/api/judge":
            return self._safely(lambda: self._routes(method, path, query,
                                                    agent, server))
        if method == "GET" and path in READ_ONLY_GETS:
            return self._routes(method, path, query, agent, server)
        # 变更操作有界等待：长心跳/聊天占锁时快速 409，而不是让浏览器挂几分钟
        if not server.mut_lock.acquire(timeout=3.0):
            return self._json(409, {"error": "agent_busy",
                                    "hint": "自学习或长任务进行中，稍后重试"})
        try:
            return self._routes(method, path, query, agent, server)
        finally:
            server.mut_lock.release()

    def _routes(self, method: str, path: str, query, agent: ProtoAgent,
                server: ProtoWebServer):
        if path == "/api/agents" and method == "GET":
            return self._json(200, {"agents": server.manager.list(),
                                    "current": server.manager.current_id()})
        if path == "/api/agents" and method == "POST":
            info = server.manager.create(**{
                k: v for k, v in self._body().items()
                if k in ("name", "persona", "priority")})
            server.manager.set_current(info["id"])
            return self._json(200, {"created": info,
                                    "agents": server.manager.list()})
        m5 = re.match(r"^/api/agents/([A-Za-z0-9_-]+)/act$", path)
        if m5 and method == "POST":
            # 异步 job：浏览器 fetch 会在约 100s 断开，完整自主链路可能跑几分钟
            target = server.manager.get(m5.group(1))
            if target is None:
                return self._json(404, {"error": "no_such_agent"})
            import threading as _th
            import uuid as _uuid
            job_id = _uuid.uuid4().hex[:12]
            server.jobs[job_id] = {"status": "running", "result": None,
                                    "agent": m5.group(1)}

            def _run():
                try:
                    with server.mut_lock:
                        result = target.force_act()
                    target._inbox.append(
                        result.get("message")
                        or f"行动未成：{result.get('reason', '')}")
                    server.jobs[job_id]["result"] = result
                    server.jobs[job_id]["status"] = "done"
                except Exception as exc:
                    server.jobs[job_id]["result"] = {"acted": False,
                                                      "reason": str(exc)[:200]}
                    server.jobs[job_id]["status"] = "error"

            _th.Thread(target=_run, daemon=True).start()
            return self._json(200, {"job": job_id, "status": "running"})
        m7 = re.match(r"^/api/jobs/([0-9a-f]+)$", path)
        if m7 and method == "GET":
            job = server.jobs.get(m7.group(1))
            if job is None:
                return self._json(404, {"error": "no_such_job"})
            return self._json(200, job)
        m4 = re.match(r"^/api/agents/([A-Za-z0-9_-]+)/(select|pause|archive)$", path)
        if m4 and method == "POST":
            aid, action = m4.group(1), m4.group(2)
            if action == "select":
                server.manager.set_current(aid)
            elif action == "pause":
                server.manager.pause(aid, bool(self._body().get("on", True)))
            elif action == "archive":
                if not server.manager.archive(aid):
                    return self._json(404, {"error": "no_such_agent"})
            return self._json(200, {"agents": server.manager.list(),
                                    "current": server.manager.current_id()})

        if path == "/api/state" and method == "GET":
            return self._json(200, _state_payload(agent, server))

        if path == "/api/chat" and method == "POST":
            body = self._body()
            reply = agent.chat(body.get("text", ""))
            self._json(200, {"reply": reply})   # 先回包，主人零等待
            agent.remember_turn(body.get("text", ""), reply)
            proposal = agent.maybe_proactive()
            if proposal:
                agent._inbox.append(proposal)   # 下轮轮询经收件箱可见
            return None

        if path == "/api/task" and method == "POST":
            return self._json(200, agent.run_task(self._body().get("text", "")))
        m3 = re.match(r"^/api/task/([A-Za-z0-9_-]+)/resume$", path)
        if m3 and method == "POST":
            result = agent.resume_task(m3.group(1), bool(self._body().get("approved")))
            return self._json(200, result)

        if path == "/api/proposals" and method == "GET":
            return self._json(200, {"proposals": agent.pending_proposals()})
        m = re.match(r"^/api/proposals/([0-9a-f]+)/(accept|deny)$", path)
        if m and method == "POST":
            pid, action = m.group(1), m.group(2)
            if action == "deny":
                threshold = agent.deny_proposal(pid)
                return self._json(200, {"denied": pid, "threshold": threshold})
            return self._json(200, agent.accept_proposal(pid))

        if path == "/api/tick" and method == "POST":
            return self._json(200, {"proposal": agent.maybe_proactive()})

        if path == "/api/audit" and method == "GET":
            n = int(query.get("n", ["20"])[0])
            return self._json(200, {
                "events": agent.audit_tail(n),
                "chain_ok": agent.spine.verify_chain().ok,
            })

        if path == "/api/mute" and method == "POST":
            on = bool(self._body().get("on"))
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
            return self._json(200, agent.set_priority(self._body().get("text", "")))
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
            agent.set_persona(self._body().get("text", ""))
            return self._json(200, {"persona": agent.persona})

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
            agent.goal_tree.drop(m6.group(1))
            return self._json(200, agent.goal_tree.snapshot())
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


def _budget_view(agent: ProtoAgent) -> dict:
    try:
        return {name: dict(units) for name, units in
                agent.kernel.pools._remaining.items()}
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
                 keep_awake=None, manager=None):
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
        self.agent = agent or manager.get(manager.current_id())  # 遗留引用
        self.token = token or secrets.token_urlsafe(16)
        from proto.system.keepawake import KeepAwake
        self.keeper = keep_awake if keep_awake is not None else KeepAwake()
        self.lock = threading.Lock()      # 遗留别名（心跳）
        self.mut_lock = threading.Lock()  # 变更长锁：chat/task/evolve
        self.jobs = {}                    # 异步任务（立即行动等长链路）
        self._hb_stop = threading.Event()
        super().__init__((host, port), _Handler)
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
                # 主人优先：锁被交互占用就跳过本轮自主节律（欲望会累积，不丢）
                if not self.mut_lock.acquire(timeout=0.2):
                    continue
                try:
                    self.manager.tick_all()      # M21：驱动整支舰队
                finally:
                    self.mut_lock.release()
            except Exception:
                pass  # 心跳失败不致命——下个周期重试

    def server_close(self) -> None:
        self._hb_stop.set()
        super().server_close()

    @property
    def port(self) -> int:
        return self.server_address[1]

    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/#token={self.token}"
