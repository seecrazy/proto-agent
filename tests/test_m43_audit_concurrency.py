# -*- coding: utf-8 -*-
"""M43：审计链并发写修复 + 断链重封。

生产事故（2026-11-18 发现）：两个进程各持一份 AuditSpine 写同一存储时，
append() 用内存缓存链头（M2 的 O(1) 优化），后写者拿陈旧链头覆写已存在
的 seq 记录 → 链断（iPhone 专家断在 16，插件专家断在 8）。
"""
import pytest

from proto.kernel.audit import AuditSpine, _record_hash
from proto.storage.store import FileSystemStore


@pytest.fixture()
def store(tmp_path):
    return FileSystemStore(tmp_path / "agent")


@pytest.fixture()
def secret():
    return b"unit-secret"


class TestConcurrentAppend:
    def test_second_process_append_reloads_head(self, store, secret):
        """进程 B 在进程 A 追加之后再 append，必须重载真实链头而不是覆写。"""
        s1 = AuditSpine(store, secret=secret)
        s2 = AuditSpine(store, secret=secret)  # 与 s1 同时加载（此刻链头一致）
        s1.append("goal_pushed", {"n": 1})
        s1.append("web_fetch", {"n": 2})
        s1.append("web_fetch", {"n": 3})
        # 旧行为：s2 仍持空链头缓存 → 覆写 seq1/2；修复后：应排到 seq4
        seq = s2.append("owner_override", {"n": 4})
        assert seq == 4
        rows = sorted(store.find("audit", lambda d: True), key=lambda r: r["seq"])
        assert [r["seq"] for r in rows] == [1, 2, 3, 4]
        assert AuditSpine(store, secret=secret).verify_chain().ok

    def test_first_process_recovers_after_external_write(self, store, secret):
        """进程 A 在进程 B 写入后再 append，同样要重载（双向防覆写）。"""
        s1 = AuditSpine(store, secret=secret)
        s2 = AuditSpine(store, secret=secret)
        s1.append("a", {})
        s2.append("b", {})   # 外部写入（修复后 s2 重载 → seq2）
        seq = s1.append("c", {})
        assert seq == 3
        assert AuditSpine(store, secret=secret).verify_chain().ok

    def test_rapid_interleave_stays_contiguous(self, store, secret):
        s1 = AuditSpine(store, secret=secret)
        s2 = AuditSpine(store, secret=secret)
        for i in range(6):
            (s1 if i % 2 == 0 else s2).append("tick", {"i": i})
        rows = sorted(store.find("audit", lambda d: True), key=lambda r: r["seq"])
        assert [r["seq"] for r in rows] == list(range(1, 7))
        assert AuditSpine(store, secret=secret).verify_chain().ok

    def test_empty_then_external_write(self, store, secret):
        """构造时链为空，之后外部进程写入，本进程 append 必须接上。"""
        s1 = AuditSpine(store, secret=secret)
        s2 = AuditSpine(store, secret=secret)
        s2.append("x", {})
        s2.append("y", {})
        assert s1.append("z", {}) == 3
        assert AuditSpine(store, secret=secret).verify_chain().ok


class TestReseal:
    def _make_fork(self, store, secret):
        """手工复刻生产事故形态：陈旧写者持 seq=2 时的链头，覆写 3、4 号
        记录（时间戳晚于被它挤掉的历史），原 5 号的 prev 悬空 → 断链。"""
        spine = AuditSpine(store, secret=secret)
        for i in range(5):
            spine.append("event", {"i": i})
        rows = sorted(store.find("audit", lambda d: True), key=lambda r: r["seq"])
        stale_prev = rows[1]["hash"]  # 陈旧进程记忆中的链头 = 原 seq2
        for seq, i in ((3, 30), (4, 31)):
            ts = "2026-11-18T22:07:42+00:00"  # 时间戳晚于原记录（乱序是事故特征）
            rec = {
                "seq": seq, "ts": ts, "event_type": "event", "payload": {"i": i},
                "prev_hash": stale_prev,
                "algorithm": "hmac-sha256",
            }
            rec["hash"] = _record_hash(seq, ts, "event", rec["payload"],
                                       stale_prev, secret=secret,
                                       algorithm="hmac-sha256")
            stale_prev = rec["hash"]
            store.put("audit", str(seq), rec)
        assert not AuditSpine(store, secret=secret).verify_chain().ok

    def test_reseal_restores_chain_and_keeps_events(self, store, secret):
        self._make_fork(store, secret)
        spine = AuditSpine(store, secret=secret)
        result = spine.reseal("unit: 复刻并发覆写事故后修复")
        assert result["ok"] is True
        fresh = AuditSpine(store, secret=secret)
        assert fresh.verify_chain().ok
        rows = sorted(store.find("audit", lambda d: True), key=lambda r: r["seq"])
        # 全部旧事件保留（5 条旧 + 1 条 chain_repair），负载不被篡改
        payloads = [r["payload"] for r in rows]
        assert {"i": 0} in payloads and {"i": 30} in payloads
        assert any(r["event_type"] == "chain_repair" for r in rows)

    def test_reseal_on_healthy_chain_is_noop(self, store, secret):
        spine = AuditSpine(store, secret=secret)
        spine.append("a", {})
        result = spine.reseal("noop check")
        assert result["ok"] is True
        assert result.get("repaired") is False
        rows = sorted(store.find("audit", lambda d: True), key=lambda r: r["seq"])
        assert len(rows) == 1  # 未追加 chain_repair

    def test_reseal_refuses_tampered_payload(self, store, secret):
        """记录自哈希不匹配（真篡改）时拒绝重封。"""
        spine = AuditSpine(store, secret=secret)
        spine.append("a", {"i": 1})
        rows = sorted(store.find("audit", lambda d: True), key=lambda r: r["seq"])
        row = rows[0]
        row["payload"] = {"i": 999}  # 改负载但不改 hash
        store.put("audit", "1", row)
        result = AuditSpine(store, secret=secret).reseal("try")
        assert result["ok"] is False
        assert "篡改" in result["reason"]
