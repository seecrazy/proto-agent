# -*- coding: utf-8 -*-
"""Task 2: 审计脊柱——只追加、哈希链、篡改可检测。"""
import pytest

from proto.kernel.audit import AuditSpine


@pytest.fixture()
def spine(tmp_path):
    from proto.state.store import FileSystemStore
    return AuditSpine(FileSystemStore(tmp_path / "data"))


def test_append_returns_increasing_seq(spine):
    assert spine.append("modulation", {"param": "exploration"}) == 1
    assert spine.append("veto", {"rule": "high_risk"}) == 2


def test_genesis_prev_hash_is_zeros(spine, tmp_path):
    from proto.state.store import FileSystemStore
    spine.append("modulation", {"x": 1})
    spine.append("veto", {"y": 2})
    store = FileSystemStore(tmp_path / "data")
    doc = store.get("audit", "1")
    assert doc["prev_hash"] == "0" * 64
    assert len(doc["hash"]) == 64


def test_chain_links(spine, tmp_path):
    from proto.state.store import FileSystemStore
    spine.append("modulation", {"x": 1})
    spine.append("veto", {"y": 2})
    store = FileSystemStore(tmp_path / "data")
    first = store.get("audit", "1")
    second = store.get("audit", "2")
    assert second["prev_hash"] == first["hash"]


def test_valid_chain_verifies(spine):
    for i in range(5):
        spine.append("modulation", {"i": i})
    result = spine.verify_chain()
    assert result.ok
    assert result.broken_at is None


def test_tampered_record_detected(spine, tmp_path):
    from proto.state.store import FileSystemStore
    for i in range(5):
        spine.append("modulation", {"i": i})
    # 直接改写存储中的第 3 条（绕过 spine）
    store = FileSystemStore(tmp_path / "data")
    doc = store.get("audit", "3")
    doc["payload"]["i"] = 999
    store.put("audit", "3", doc)
    result = spine.verify_chain()
    assert not result.ok
    assert result.broken_at == 3


def test_deleted_record_detected(spine, tmp_path):
    from proto.state.store import FileSystemStore
    for i in range(5):
        spine.append("modulation", {"i": i})
    store = FileSystemStore(tmp_path / "data")
    store.delete("audit", "3")
    result = spine.verify_chain()
    assert not result.ok
    assert result.broken_at == 3


def test_tail_returns_last_n(spine):
    for i in range(10):
        spine.append("modulation", {"i": i})
    rows = spine.tail(3)
    assert [r["seq"] for r in rows] == [8, 9, 10]


def test_persistence_across_instances(tmp_path):
    from proto.state.store import FileSystemStore
    store = FileSystemStore(tmp_path / "data")
    s1 = AuditSpine(store)
    s1.append("modulation", {"a": 1})
    s2 = AuditSpine(store)
    assert s2.append("veto", {"b": 2}) == 2
    assert s2.verify_chain().ok
