# -*- coding: utf-8 -*-
"""M36: 安全/持久化/运行时一致性加固——验收清单逐条锁死。

对应 docs/specs/2026-11-18-hardening.md §6：
- 工具能力策略只可加严不可降级；副作用工具统一审批
- 工作区 symlink 逃逸拒绝；无隔离后端执行失败关闭
- 审批/预算/白名单/暂停任务/记忆元数据跨重启保持
- 审计链被外部篡改后验证失败
- max_steps 不奖励；超大文件编辑拒绝
- 空测试集不产 canary；晋升须持真实沙盒通过记录
- Web：安全头 / 413 / 400 / HTML 注入面收敛
"""
import json
import os
import stat
import threading
import urllib.request

import pytest

from proto.exec.codetools import CodeTools, MAX_EDIT
from proto.exec.router import Router, Task, ToolRegistry
from proto.exec.workspace import ScratchWorkspace
from proto.kernel.audit import AuditSpine
from proto.kernel.budgets import BudgetPools
from proto.kernel.kernel import ActionCategory, Kernel
from proto.llm.client import LLMClient
from proto.runtime.agent import DEFAULT_POOLS, ProtoAgent
from proto.state.store import FileSystemStore


class Null(LLMClient):
    def complete(self, system, user):
        return "（离线）"


def dead_web():
    from proto.exec.webtools import WebToolkit
    return WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))


def make_agent(tmp_path, **kw):
    return ProtoAgent(data_dir=tmp_path / "data", llm=Null(),
                      goal_interval_s=10 ** 9, webtools=dead_web(), **kw)


@pytest.fixture()
def agent(tmp_path):
    return make_agent(tmp_path)


# ---------------------------------------------------------------- 策略
class TestCapabilityPolicy:
    def test_non_write_tool_marked_visible_write_pauses(self, agent):
        """非 write_file 工具被（模型或任务）标成 visible_write → 暂停等审批。"""
        task = Task(id="vw1", description="重活", steps=[
            {"tool": "delegate_coding", "args": {"task": "改代码"},
             "category": "visible_write"},
        ])
        if "delegate_coding" not in agent.tools.names():
            pytest.skip("无外部编码 CLI")
        result = agent.router.run(task)
        assert result.status == "paused"
        assert agent.queue.pending()

    def test_model_cannot_downgrade_policy_category(self, agent):
        """注册为 VISIBLE_WRITE 的工具，模型标 read_draft 仍按工具策略审批。"""
        agent.tools.register("danger", lambda: "boom",
                             category=ActionCategory.VISIBLE_WRITE,
                             description="高危工具")
        task = Task(id="dg1", description="降级尝试", steps=[
            {"tool": "danger", "args": {}, "category": "read_draft"},
        ])
        result = agent.router.run(task)
        assert result.status == "paused"          # 仍是审批级，未降级

    def test_run_python_downgrade_still_gated_under_subprocess(self,
                                                               tmp_path,
                                                               monkeypatch):
        """subprocess（未隔离）后端下 run_python 策略=VISIBLE_WRITE：
        标 read_draft 也必须暂停。"""
        monkeypatch.setenv("PROTO_EXEC_BACKEND", "subprocess")
        a = make_agent(tmp_path)
        task = Task(id="rp1", description="跑代码", steps=[
            {"tool": "run_python", "args": {"code": "print(1)"},
             "category": "read_draft"},
        ])
        result = a.router.run(task)
        assert result.status == "paused"

    def test_illegal_category_fails_task(self, agent):
        task = Task(id="bad1", description="坏类别", steps=[
            {"tool": "clock", "args": {}, "category": "omnipotent"},
        ])
        result = agent.router.run(task)
        assert result.status == "failed"
        assert "非法动作分级" in result.reason

    def test_tool_exec_audited(self, agent):
        agent.router.run(Task(id="au1", description="看时间", steps=[
            {"tool": "clock", "args": {}, "category": "read_draft"}]))
        events = agent.spine.find(lambda d: d["event_type"] == "tool_exec")
        assert events and events[-1]["payload"]["tool"] == "clock"


# ---------------------------------------------------------------- 边界
class TestWorkspaceBoundary:
    def test_codetools_symlink_escape_rejected(self, tmp_path):
        ct = CodeTools(tmp_path / "ws")
        ct.write_file("in.txt", "ok")
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        os.symlink(outside, tmp_path / "ws" / "evil.txt")
        with pytest.raises(ValueError):
            ct.read_file("evil.txt")
        with pytest.raises(ValueError):
            ct.write_file("evil.txt", "tamper")

    def test_scratch_workspace_symlink_escape_rejected(self, tmp_path):
        ws = ScratchWorkspace(tmp_path / "ws")
        outside = tmp_path / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        # 直接在 scratch 内放一个指向外部的符号链接
        scratch_root = tmp_path / "ws" / "scratch"
        os.symlink(outside, scratch_root / "evil.txt")
        with pytest.raises(ValueError):
            ws.write("evil.txt", "tamper")

    def test_run_python_fail_closed_without_backend(self, tmp_path,
                                                    monkeypatch):
        monkeypatch.setenv("PROTO_EXEC_BACKEND", "none")
        ct = CodeTools(tmp_path / "ws")
        r = ct.run_python("print('must not run')")
        assert r["exit"] == -1 and "execution_disabled" in r["stderr"]

    def test_edit_file_oversize_refused(self, tmp_path):
        ct = CodeTools(tmp_path / "ws")
        ct.write_file("big.txt", "x" * (MAX_EDIT + 10))
        size_before = (tmp_path / "ws" / "big.txt").stat().st_size
        with pytest.raises(ValueError):
            ct.edit_file("big.txt", "x", "y", replace_all=True)
        assert (tmp_path / "ws" / "big.txt").stat().st_size == size_before


# ---------------------------------------------------------------- 持久化
class TestDurableRuntime:
    def test_pending_approval_survives_restart(self, tmp_path):
        a1 = make_agent(tmp_path)
        result = a1.router.run(Task(id="pp1", description="产出", steps=[
            {"tool": "write_file", "args": {"path": "doc.md", "content": "hi"},
             "category": "visible_write"}]))
        assert result.status == "paused"
        a2 = make_agent(tmp_path)                        # 重启（同 data_dir）
        pending = a2.queue.pending()
        assert len(pending) == 1 and "doc.md" in pending[0].description
        final = a2.resume_task("pp1", approved=True)     # 恢复用已恢复的审批
        assert final["status"] == "completed"
        assert a2.workspace.list_visible() == ["doc.md"]

    def test_budget_survives_restart(self, tmp_path):
        a1 = make_agent(tmp_path)
        a1.kernel.pools.try_consume("self_initiated", {"tokens": 500.0})
        a2 = make_agent(tmp_path)
        assert a2.kernel.pools.remaining(
            "self_initiated", "tokens") == DEFAULT_POOLS["self_initiated"]["tokens"] - 500.0

    def test_auto_quota_survives_restart(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROTO_AUTO_APPROVE_N", "1")
        a1 = make_agent(tmp_path)
        assert a1.auto_quota.try_consume()               # 用掉唯一的配额
        a2 = make_agent(tmp_path)
        assert not a2.auto_quota.try_consume()           # 重启不能重置配额
        assert a2.auto_quota.remaining() == 0

    def test_memory_metadata_survives_restart(self, tmp_path):
        a1 = make_agent(tmp_path)
        m = a1.memory.add("episode", "难忘的一天", importance=0.9,
                          emotion={"pleasure": 0.8, "arousal": 0.5,
                                   "dominance": 0.3})
        a1.memory.reinforce(m.id)
        a2 = make_agent(tmp_path)
        loaded = a2.memory._memories[m.id]
        assert loaded.emotion["pleasure"] == 0.8
        assert loaded.reinforce_count == 1

    def test_goal_acceptance_survives_restart(self, tmp_path):
        a1 = make_agent(tmp_path)
        node = a1.goal_tree.add("做一个加固验证", level="mid",
                                acceptance="验收：测试全绿")
        a2 = make_agent(tmp_path)
        assert a2.goal_tree.get(node.id).acceptance == "验收：测试全绿"


# ---------------------------------------------------------------- 审计
class TestAuditTamper:
    def test_external_tamper_breaks_chain(self, tmp_path):
        store = FileSystemStore(tmp_path / "d")
        spine = AuditSpine(store, secret=b"\x02" * 32)
        spine.append("proposal", {"x": 1})
        spine.append("approval", {"y": 2})
        assert spine.verify_chain().ok
        # 外部改写第 1 条记录（绕过 spine 直接写存储）
        record = store.get("audit", "1")
        record["payload"] = {"x": 999}
        store.put("audit", "1", record)
        result = spine.verify_chain()
        assert not result.ok and result.broken_at == 1

    def test_hmac_record_requires_secret(self, tmp_path):
        store = FileSystemStore(tmp_path / "d")
        spine = AuditSpine(store, secret=b"\x02" * 32)
        spine.append("proposal", {"x": 1})
        record = store.get("audit", "1")
        assert record["algorithm"] == "hmac-sha256"
        # 无 secret 的验证者无法伪造匹配哈希 → 验证失败
        stranger = AuditSpine(FileSystemStore(tmp_path / "d2"))
        stranger.append("proposal", {"x": 1})
        with pytest.raises(ValueError):
            from proto.kernel.audit import _record_hash
            _record_hash(1, "t", "proposal", {"x": 1}, "0" * 64,
                         algorithm="hmac-sha256")


# ---------------------------------------------------------------- 记账
class TestTaskAccounting:
    def test_max_steps_no_reward_no_internalize(self, agent):
        agent.instruct("每次交付必须先跑测试")
        before = agent.state.desires["curiosity_gap"]["value"]
        agent._on_task_completed(type("R", (), {"status": "max_steps",
                                                "retries": 0, "reason": ""})())
        after = agent.state.desires["curiosity_gap"]["value"]
        assert after == before                       # 不降低欲望
        rule = agent.ledger.get_all()[0]
        assert rule["applications"] == []            # 不触发规矩内化

    def test_completed_still_rewards(self, agent):
        before = agent.state.desires["curiosity_gap"]["value"]
        agent._on_task_completed(type("R", (), {"status": "completed",
                                                "retries": 0, "reason": ""})())
        assert agent.state.desires["curiosity_gap"]["value"] < before


# ---------------------------------------------------------------- 进化
class TestEvolutionGates:
    def _evo(self, agent):
        evo = agent._evolution()
        return evo["engine"], evo["archive"]

    def test_empty_testset_never_canary(self, tmp_path):
        agent = make_agent(tmp_path)
        result = agent.evolve("def solve(x):\n    return x\n")
        assert not result["merged"] and result["reason"] == "empty_testset"

    def test_promote_requires_real_sandbox_record(self, tmp_path):
        agent = make_agent(tmp_path)
        engine, _ = self._evo(agent)

        class AlwaysAllow:
            def authorize(self, change, sandbox_passed=False):
                return type("D", (), {"allowed": True})()

        # 没有任何沙盒通过记录 → 即使授权者放行也拒绝
        assert not engine.promote("nonexistent", AlwaysAllow(), "cls", True)
        # 真实通过 → 放行
        agent.add_test_case(1, 1)
        agent.add_test_case(2, 2)
        decision = engine.evaluate("def solve(x):\n    return x\n", None)
        assert decision.merged
        assert engine.promote(decision.variant_id, AlwaysAllow(), "cls", True)


# ---------------------------------------------------------------- Web
class TestWebHardening:
    def _server(self, tmp_path):
        from proto.web.server import ProtoWebServer
        server = ProtoWebServer(agent=make_agent(tmp_path), port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def _raw(self, server, method, path, data=None, headers=None):
        req = urllib.request.Request(
            f"http://127.0.0.1:{server.port}{path}", data=data, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    def test_security_headers_on_index_and_api(self, tmp_path):
        server = self._server(tmp_path)
        try:
            status, headers, _ = self._raw(server, "GET", "/")
            assert status == 200
            assert "default-src 'self'" in headers.get("Content-Security-Policy", "")
            assert headers.get("X-Content-Type-Options") == "nosniff"
            status, headers, _ = self._raw(
                server, "GET", "/api/state",
                headers={"X-Proto-Token": server.token})
            assert status == 200
            assert headers.get("X-Content-Type-Options") == "nosniff"
            assert headers.get("X-Frame-Options") == "DENY"
        finally:
            server.shutdown()
            server.server_close()

    def test_invalid_json_returns_400(self, tmp_path):
        server = self._server(tmp_path)
        try:
            status, _, body = self._raw(
                server, "POST", "/api/chat", data=b"{not json",
                headers={"X-Proto-Token": server.token,
                         "Content-Type": "application/json"})
            assert status == 400 and b"invalid_json" in body
            status, _, body = self._raw(
                server, "POST", "/api/chat", data=b"[1,2]",
                headers={"X-Proto-Token": server.token,
                         "Content-Type": "application/json"})
            assert status == 400                            # 非对象 JSON
        finally:
            server.shutdown()
            server.server_close()

    def test_oversized_body_returns_413(self, tmp_path):
        from proto.web.server import MAX_BODY_BYTES
        server = self._server(tmp_path)
        try:
            status, _, body = self._raw(
                server, "POST", "/api/chat",
                data=b'{"text": "' + b"a" * (MAX_BODY_BYTES + 100) + b'"}',
                headers={"X-Proto-Token": server.token,
                         "Content-Type": "application/json"})
            assert status == 413 and b"body_too_large" in body
        finally:
            server.shutdown()
            server.server_close()

    def test_index_html_escapes_untrusted_interpolations(self):
        """注入面收敛：路径/文本类字段一律 esc() 后再进 innerHTML。"""
        source = (os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                  + "/proto/web/index.html")
        text = open(source, encoding="utf-8").read()
        for risky in ("${n.path}", "${n.level}", "${p.status}"):
            assert risky not in text, f"未转义插值残留: {risky}"


# ---------------------------------------------------------------- 单实例
class TestInstanceLock:
    def test_second_instance_refused(self, tmp_path):
        from proto.system.instance_lock import InstanceLock
        first = InstanceLock(tmp_path)
        assert first.acquire()
        assert not InstanceLock(tmp_path).acquire()
        first.release()
        assert InstanceLock(tmp_path).acquire()
