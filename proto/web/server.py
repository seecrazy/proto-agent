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

READ_ONLY_GETS = {
    "/api/state", "/api/audit", "/api/proposals", "/api/rules",
    "/api/evolution", "/api/scratch", "/api/memory", "/api/inbox",
}


class _Handler(BaseHTTPRequestHandler):
    server_version = "ProtoWeb/0.16.0"

    def log_message(self, fmt, *args):
        pass

    # ---- 基础设施 ----
    def _json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        supplied = self.headers.get("X-Proto-Token") or ""
        return hmac.compare_digest(supplied, self.server.token)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            return {}

    def _safely(self, fn):
        try:
            return fn()
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
        agent: ProtoAgent = server.agent
        path, query = parsed.path, parse_qs(parsed.query)
        if method == "GET" and path in READ_ONLY_GETS:
            return self._routes(method, path, query, agent, server)
        with server.mut_lock:
            return self._routes(method, path, query, agent, server)

    def _routes(self, method: str, path: str, query, agent: ProtoAgent,
                server: ProtoWebServer):
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

        if path == "/api/memory" and method == "GET":
            return self._json(200, agent.memory.snapshot())
        if path == "/api/memory/forget" and method == "POST":
            agent.forget_memory(self._body().get("id", ""))
            return self._json(200, agent.memory.snapshot())

        if path == "/api/inbox" and method == "GET":
            return self._json(200, {"messages": agent.pop_inbox()})

        if path == "/api/scratch" and method == "GET":
            notes = []
            for rel in agent.workspace.list_scratch():
                if not rel.startswith("notes/"):
                    continue
                try:
                    content = (agent.workspace._scratch / rel).read_text(encoding="utf-8")
                except OSError:
                    continue
                notes.append({"path": rel, "preview": content[:160]})
            return self._json(200, {"notes": notes[-10:]})

        if path == "/api/rules" and method == "GET":
            return self._json(200, {"rules": agent.rules_view()})
        if path == "/api/rules" and method == "POST":
            rid = agent.instruct(self._body().get("text", ""))
            return self._json(200, {"rule_id": rid, "rules": agent.rules_view()})
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

    def __init__(self, agent: ProtoAgent, host: str = "127.0.0.1", port: int = 8420,
                 token: str = None, heartbeat_s: float = 30.0, keep_awake=None):
        self.agent = agent
        self.token = token or secrets.token_urlsafe(16)
        from proto.system.keepawake import KeepAwake
        self.keeper = keep_awake if keep_awake is not None else KeepAwake()
        self.lock = threading.Lock()      # 遗留别名（心跳）
        self.mut_lock = threading.Lock()  # 变更长锁：chat/task/evolve
        self._hb_stop = threading.Event()
        super().__init__((host, port), _Handler)
        if heartbeat_s > 0:
            threading.Thread(target=self._heartbeat, args=(heartbeat_s,),
                             daemon=True, name="proto-heartbeat").start()

    def _heartbeat(self, interval: float) -> None:
        while not self._hb_stop.wait(interval):
            try:
                with self.mut_lock:
                    self.agent.maybe_proactive()
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
