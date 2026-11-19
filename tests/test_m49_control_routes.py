# -*- coding: utf-8 -*-
"""M49：主人控制操作不被自主长链卡死（用户报"点防休眠报 agent_busy"）。

设计倒置修复：静音/防休眠是长链运行中最需要的紧急控制，却要跟链抢
同一把 mut_lock（链一跑几分钟）→ 控制类 POST 免锁直通；审计脊柱加
线程锁保证免锁后的并发 append 安全（M43 只防了跨进程，进程内两线程
共享同一 spine 实例仍有读-改-写竞态）。
"""
import json
import threading
import urllib.request

import pytest

from proto.llm.client import LLMClient
from proto.web.server import ProtoWebServer


class ScriptedLLM(LLMClient):
    def complete(self, system, user):
        return "好的主人。"


@pytest.fixture()
def server(tmp_path):
    from proto.runtime.agent import ProtoAgent
    agent = ProtoAgent(data_dir=tmp_path / "web", llm=ScriptedLLM(),
                       goal_interval_s=0)
    srv = ProtoWebServer(agent, host="127.0.0.1", port=0, heartbeat_s=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def call(srv, method, path, body=None):
    url = f"http://127.0.0.1:{srv.port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    req.add_header("X-Proto-Token", srv.token)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


class TestControlRoutesLockFree:
    def test_awake_works_while_chain_holds_lock(self, server):
        """长链占锁期间点防休眠：立即成功而非 409。"""
        server.mut_lock.acquire()          # 模拟自主链占锁
        try:
            r = call(server, "POST", "/api/awake", {"on": True})
            assert "awake" in r
        finally:
            server.mut_lock.release()

    def test_mute_works_while_chain_holds_lock(self, server):
        server.mut_lock.acquire()
        try:
            r = call(server, "POST", "/api/mute", {"on": True})
            assert r.get("muted") is True
            r2 = call(server, "POST", "/api/mute", {"on": False})
            assert r2.get("muted") is False
        finally:
            server.mut_lock.release()

    def test_state_change_still_guarded(self, server):
        """普通变更操作仍走锁（防并发写坏状态）——占用时 409。"""
        server.mut_lock.acquire()
        try:
            with pytest.raises(Exception) as ei:
                call(server, "POST", "/api/priority", {"text": "x"})
            assert "agent_busy" in str(ei.value) or "HTTP" in str(ei.value)
        finally:
            server.mut_lock.release()


class TestSpineThreadSafety:
    def test_concurrent_thread_appends_no_seq_loss(self, tmp_path):
        """进程内两线程并发 append：seq 连续不覆写（原仅靠 mut_lock 串行，
        免锁路由后必须由 spine 自己保证）。"""
        from proto.storage.store import FileSystemStore
        from proto.kernel.audit import AuditSpine
        spine = AuditSpine(FileSystemStore(tmp_path / "a"), secret=b"k")
        barrier = threading.Barrier(4)
        errors = []

        def worker(n):
            barrier.wait()
            for i in range(25):
                try:
                    spine.append("tick", {"n": n, "i": i})
                except Exception as exc:   # pragma: no cover
                    errors.append(exc)

        threads = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        rows = spine.find(lambda d: True)
        assert len(rows) == 100                      # 无覆写丢失
        assert [r["seq"] for r in rows] == list(range(1, 101))
        assert spine.verify_chain().ok
