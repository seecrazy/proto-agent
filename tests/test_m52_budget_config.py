# -*- coding: utf-8 -*-
"""M52：额度数值+单位全可配（用户"自主池 8k 太少，数值和单位都支持配置"）。

- 单位后缀解析：8k / 16万 / 1.2M / 裸数字（env 与 API 通用）
- 上限持久化：控制台改的上限重启后保持（此前只认 env/默认）
- 控制台直改立即生效：上调即补余量、下调即夹紧（不必等次日 0 点）
"""
import json
import threading
import urllib.request

import pytest

from proto.kernel.budgets import parse_amount
from proto.llm.client import LLMClient
from proto.web.server import ProtoWebServer


class ScriptedLLM(LLMClient):
    def complete(self, system, user):
        return "好的。"


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
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{srv.port}{path}", data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    req.add_header("X-Proto-Token", srv.token)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


class TestParseAmount:
    def test_units(self):
        assert parse_amount("8k") == 8000
        assert parse_amount("16万") == 160000
        assert parse_amount("1.2M") == 1200000
        assert parse_amount("200000") == 200000.0
        assert parse_amount(" 50K ") == 50000
        assert parse_amount("0.5w") == 5000

    def test_invalid(self):
        assert parse_amount("abc") is None
        assert parse_amount("") is None
        assert parse_amount("-5k") is None
        assert parse_amount(None) is None


class TestSetCap:
    def test_raise_topps_up_immediately(self, tmp_path):
        from proto.kernel.budgets import BudgetPools
        from proto.storage.store import FileSystemStore
        pools = BudgetPools({"global": {"tokens": 200000.0},
                             "self_initiated": {"tokens": 8000.0}},
                            store=FileSystemStore(tmp_path))
        pools.set_cap("self_initiated", "tokens", 50000)
        assert pools._config["self_initiated"]["tokens"] == 50000
        assert pools.remaining_all()["self_initiated"]["tokens"] == 50000

    def test_lower_clamps_remaining(self, tmp_path):
        from proto.kernel.budgets import BudgetPools
        from proto.storage.store import FileSystemStore
        pools = BudgetPools({"global": {"tokens": 200000.0},
                             "owner_tasks": {"tokens": 160000.0}},
                            store=FileSystemStore(tmp_path))
        pools.set_cap("owner_tasks", "tokens", 20000)
        assert pools.remaining_all()["owner_tasks"]["tokens"] == 20000

    def test_caps_persist_across_reload(self, tmp_path):
        from proto.kernel.budgets import BudgetPools
        from proto.storage.store import FileSystemStore
        store = FileSystemStore(tmp_path)
        pools = BudgetPools({"global": {"tokens": 200000.0},
                             "self_initiated": {"tokens": 8000.0}},
                            store=store)
        pools.set_cap("self_initiated", "tokens", 30000)
        reloaded = BudgetPools({"global": {"tokens": 200000.0},
                                "self_initiated": {"tokens": 8000.0}},
                               store=store)
        assert reloaded._config["self_initiated"]["tokens"] == 30000

    def test_unknown_pool_rejected(self, tmp_path):
        from proto.kernel.budgets import BudgetPools
        pools = BudgetPools({"global": {"tokens": 100.0}})
        with pytest.raises(KeyError):
            pools.set_cap("nope", "tokens", 5)


class TestConsoleApi:
    def test_caps_endpoint_accepts_units(self, server):
        r = call(server, "POST", "/api/budget/caps",
                 {"self_initiated_tokens": "50k",
                  "owner_tasks_tokens": "20万"})
        assert r["caps"]["self_initiated"]["tokens"] == 50000
        assert r["caps"]["owner_tasks"]["tokens"] == 200000
        agent = server.manager.get(server.manager.current_id())
        assert agent.kernel.pools.remaining_all()["self_initiated"]["tokens"] \
            == 50000
        events = [e["event_type"] for e in agent.spine.find(lambda d: True)]
        assert "owner_override" in events

    def test_caps_fast_path_while_locked(self, server):
        server.manager.ops_lock(server.manager.current_id()).acquire()
        server.mut_lock.acquire()
        try:
            r = call(server, "POST", "/api/budget/caps",
                     {"self_initiated_tokens": "10k"})
            assert r["caps"]["self_initiated"]["tokens"] == 10000
        finally:
            server.mut_lock.release()
            server.manager.ops_lock(server.manager.current_id()).release()

    def test_invalid_amount_rejected(self, server):
        with pytest.raises(Exception):
            call(server, "POST", "/api/budget/caps",
                 {"self_initiated_tokens": "abc"})

    @pytest.mark.parametrize("invalid", [{"global_tokens": "abc"},
                                        {"ghost_tokens": "1k"}])
    def test_invalid_batch_does_not_partially_apply(self, server, invalid):
        pools = server.agent.kernel.pools
        before = pools.remaining_all()
        with pytest.raises(urllib.error.HTTPError) as error:
            call(server, "POST", "/api/budget/caps",
                 {"owner_tasks_tokens": "1k", **invalid})
        assert error.value.code == 400
        assert pools.remaining_all() == before


class TestEnvUnits:
    def test_env_accepts_unit_suffix(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROTO_BUDGET_SELF_TOKENS", "50k")
        from proto.runtime.agent import ProtoAgent
        a = ProtoAgent(data_dir=tmp_path / "ag")
        assert a.kernel.pools._config["self_initiated"]["tokens"] == 50000
