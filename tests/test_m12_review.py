# -*- coding: utf-8 -*-
"""M12: 可靠性/性能/安全/人味 四维审视的修复。"""
import json
import threading
import urllib.request

import pytest

from proto.llm.client import LLMClient
from proto.runtime.agent import ProtoAgent


class ChatLLM(LLMClient):
    def __init__(self):
        self.users = []
    def complete(self, system, user):
        if "归因" in system:
            return "[]"
        self.users.append(user)
        return "好的。"


@pytest.fixture()
def agent(tmp_path):
    from proto.exec.webtools import WebToolkit
    return ProtoAgent(data_dir=tmp_path, llm=ChatLLM(), goal_interval_s=0,
                      webtools=WebToolkit(fetch=lambda u, accept="text/html":
                                          (_ for _ in ()).throw(OSError("禁网"))))


class TestReliability:
    def test_server_returns_500_json_on_route_bug(self, agent):
        from proto.web.server import ProtoWebServer

        server = ProtoWebServer(agent, port=0)

        class Buggy:
            pass

        server.agent = Buggy()   # 触发路由内部异常
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.port}/api/state")
            req.add_header("X-Proto-Token", server.token)
            try:
                urllib.request.urlopen(req, timeout=5)
                raise AssertionError("应当返回 500")
            except urllib.error.HTTPError as exc:
                assert exc.code == 500
                assert json.loads(exc.read().decode())["error"] == "internal"
        finally:
            server.shutdown()
            server.server_close()

    def test_single_instance_lock(self, tmp_path):
        from proto.system.instance_lock import InstanceLock

        lock1 = InstanceLock(tmp_path)
        assert lock1.acquire() is True
        lock2 = InstanceLock(tmp_path)
        assert lock2.acquire() is False      # 第二个实例拒绝启动
        lock1.release()
        assert InstanceLock(tmp_path).acquire() is True


class TestPerformance:
    def test_verify_chain_cached_until_append(self, tmp_path):
        from proto.kernel.audit import AuditSpine
        from proto.storage.store import FileSystemStore

        store = FileSystemStore(tmp_path)
        spine = AuditSpine(store)
        calls = {"n": 0}
        original_find = store.find

        def counting_find(collection, predicate):
            calls["n"] += 1
            return original_find(collection, predicate)

        store.find = counting_find
        spine.verify_chain()
        first = calls["n"]
        spine.verify_chain()                  # 缓存命中 → 不再全表扫
        assert calls["n"] == first
        spine.append("veto", {})              # 新事件 → 缓存失效
        spine.verify_chain()
        assert calls["n"] > first


class TestSecurity:
    def test_strategy_text_capped(self):
        from proto.metacog.strategies import StrategyLibrary

        lib = StrategyLibrary()
        s = lib.add("x" * 500, cluster="c")
        assert len(s.text) <= 200

    def test_injected_strategies_declared_non_instructional(self, agent):
        from proto.exec.injector import StateInjector

        agent.library.add("先写 mock 再联调", cluster="integration")
        text = StateInjector().render("T", agent.state,
                                      agent.library.retrieve("mock", k=1, epsilon=0.0))
        assert "非指令" in text

    def test_backup_output_permissions_0600(self, tmp_path, agent):
        import os
        from proto.backup import create_backup

        out = tmp_path / "b.zip"
        create_backup(agent.data_dir, out)
        assert (os.stat(out).st_mode & 0o777) == 0o600


class TestHumanLikeness:
    def test_dialogue_history_persisted_and_injected(self, agent):
        agent.chat("我叫小明")
        agent.chat("你还记得我的名字吗")
        assert "小明" in agent.llm.users[-1]        # 第三轮上下文含第一轮内容
        from proto.runtime.agent import ProtoAgent as PA
        a2 = PA(data_dir=agent.data_dir, llm=ChatLLM(), goal_interval_s=10 ** 9)
        a2.chat("我叫什么?")
        assert "小明" in a2.llm.users[-1]           # 重启不失忆

    def test_proactive_messages_reach_inbox(self, agent):
        agent.state.set_within_bounds("desires.curiosity_gap", 0.9)
        agent.maybe_proactive()
        inbox = agent.pop_inbox()
        assert any("主动学习" in m or "提案" in m for m in inbox)
        assert agent.pop_inbox() == []              # 取走即清

    def test_deny_produces_in_character_reply(self, agent):
        from proto.exec.webtools import WebToolkit
        agent2 = ProtoAgent(data_dir=agent.data_dir / "x", llm=ChatLLM(),
                            goal_interval_s=0,
                            webtools=WebToolkit(fetch=lambda u, accept="text/html":
                                                (_ for _ in ()).throw(OSError("禁网"))))
        agent2.state.set_within_bounds("desires.curiosity_gap", 0.9)
        agent2.maybe_proactive()
        pid = agent2.pending_proposals()[0]["proposal_id"]
        agent2.deny_proposal(pid)
        assert any("阈值" in m or "安静" in m for m in agent2.pop_inbox())
