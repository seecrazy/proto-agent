# -*- coding: utf-8 -*-
"""M50：D1 偿还——按 agent 加锁 + 主人快通道，锁不再覆盖跨 agent。

旧结构两宗罪：① mut_lock 是服务器全局——一个 agent 的分钟级链挡住
全舰队所有操作；② 主人快速写操作（充值/删目标/设优先级）与长链抢
同一把锁。新结构：per-agent ops_lock（同 agent 长操作串行=单写者）+
单资源锁（BudgetPools/GoalTree/ApprovalQueue）支撑快通道直通。
"""
import json
import threading
import urllib.request

import pytest

from proto.llm.client import LLMClient
from proto.web.server import ProtoWebServer


class ScriptedLLM(LLMClient):
    def __init__(self):
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        if "分解" in system or "目标" in system or "seed" in user:
            return json.dumps([{"text": "从主人优先级长出的目标", "level": "long"}])
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


def call(srv, method, path, body=None, agent_header=None):
    url = f"http://127.0.0.1:{srv.port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    req.add_header("X-Proto-Token", srv.token)
    if agent_header:
        req.add_header("X-Proto-Agent", agent_header)
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


class TestPerAgentIsolation:
    def test_other_agent_not_blocked_by_chain(self, server):
        """A 的链占着 A 的锁，B 的操作照常（原来全局锁挡全舰队）。
        加固：同时占住全局 mut_lock 与 A 的锁——旧实现（全局锁）必 409，
        新实现（per-agent）B 照常。"""
        info = server.manager.create("B", persona="", priority="")
        a_id = server.manager.current_id()
        lock_a = server.manager.ops_lock(a_id)
        lock_a.acquire()
        server.mut_lock.acquire()
        try:
            r = call(server, "POST", "/api/persona", {"text": "测试人设"},
                     agent_header=info["id"])
            assert r.get("persona") == "测试人设"
        finally:
            server.mut_lock.release()
            lock_a.release()

    def test_refill_fast_path_while_busy(self, server):
        aid = server.manager.current_id()
        server.manager.ops_lock(aid).acquire()
        server.mut_lock.acquire()
        try:
            r = call(server, "POST", "/api/budget/refill", {})
            assert r.get("refilled") is True
        finally:
            server.mut_lock.release()
            server.manager.ops_lock(aid).release()

    def test_drop_goal_fast_path_while_busy(self, server):
        agent = server.manager.get(server.manager.current_id())
        node = agent.goal_tree.add("待删目标", "mid")
        server.manager.ops_lock(server.manager.current_id()).acquire()
        server.mut_lock.acquire()
        try:
            r = call(server, "POST", f"/api/goals/{node.id}/drop", {})
            assert any(n["id"] == node.id for n in r.get("nodes", [])
                       if n.get("status") != "dropped") is False
        finally:
            server.mut_lock.release()
            server.manager.ops_lock(server.manager.current_id()).release()

    def test_priority_fast_path_defers_seeding(self, server):
        """链运行时设优先级：文本立即生效，目标播种延到空闲（不抢锁）。"""
        agent = server.manager.get(server.manager.current_id())
        server.manager.ops_lock(server.manager.current_id()).acquire()
        server.mut_lock.acquire()
        try:
            r = call(server, "POST", "/api/priority",
                     {"text": "补齐支付模块单测"})
            assert r.get("owner_priority") == "补齐支付模块单测"
            assert agent.state.owner_priority == "补齐支付模块单测"
            assert getattr(agent, "_priority_seed_dirty", False) is True
            assert not agent.goal_tree.all_active()   # 播种还没发生
        finally:
            server.mut_lock.release()
            server.manager.ops_lock(server.manager.current_id()).release()
        # 空闲后补播种（心跳会做；这里直接调）
        agent.seed_priority_goals()
        assert agent.goal_tree.all_active()
        assert getattr(agent, "_priority_seed_dirty", False) is False


class TestResourceLocks:
    def test_budget_pools_concurrent_consume_refill(self, tmp_path):
        from proto.kernel.budgets import BudgetPools
        from proto.storage.store import FileSystemStore
        cfg = {"global": {"tokens": 10000.0},
               "owner_tasks": {"tokens": 5000.0}}
        pools = BudgetPools(cfg, store=FileSystemStore(tmp_path))
        barrier = threading.Barrier(8)
        errors = []

        def consumer():
            barrier.wait()
            for _ in range(30):
                d = pools.try_consume("owner_tasks", {"tokens": 10})
                if not d.granted:
                    errors.append("denied")

        def refiller():
            barrier.wait()
            for _ in range(5):
                pools.refill()

        ts = ([threading.Thread(target=consumer) for _ in range(7)]
              + [threading.Thread(target=refiller)])
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert not errors
        rem = pools.remaining_all()["owner_tasks"]["tokens"]
        assert 0 <= rem <= 5000.0   # 无负数无爆表

    def test_goal_tree_concurrent_push(self, tmp_path):
        from proto.goals.system import GoalTree
        from proto.storage.store import FileSystemStore
        tree = GoalTree(FileSystemStore(tmp_path / "g"))
        barrier = threading.Barrier(4)
        errors = []

        def pusher(n):
            barrier.wait()
            for i in range(20):
                try:
                    tree.add(f"目标{n}-{i}", "mid")
                except Exception as exc:
                    errors.append(exc)

        ts = [threading.Thread(target=pusher, args=(k,)) for k in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert not errors
        assert len(tree.all_active()) == 80      # 无丢失
