# -*- coding: utf-8 -*-
"""Task 1: DocumentStore / FileSystemStore 行为测试。"""
import threading

import pytest

from proto.state.store import FileSystemStore


@pytest.fixture()
def store(tmp_path):
    return FileSystemStore(tmp_path / "data")


def test_put_get_roundtrip(store):
    doc = {"agent_id": "proto-001", "value": 0.6}
    store.put("state", "proto-001", doc)
    assert store.get("state", "proto-001") == doc


def test_get_missing_returns_none(store):
    assert store.get("state", "nope") is None


def test_put_overwrites_same_id(store):
    store.put("state", "a", {"v": 1})
    store.put("state", "a", {"v": 2})
    assert store.get("state", "a") == {"v": 2}


def test_find_filters_by_predicate(store):
    store.put("audit", "1", {"seq": 1, "type": "modulation"})
    store.put("audit", "2", {"seq": 2, "type": "veto"})
    store.put("audit", "3", {"seq": 3, "type": "modulation"})
    rows = store.find("audit", lambda d: d["type"] == "modulation")
    assert [r["seq"] for r in rows] == [1, 3]


def test_delete_removes_document(store):
    store.put("state", "a", {"v": 1})
    store.delete("state", "a")
    assert store.get("state", "a") is None
    store.delete("state", "a")  # 幂等


def test_find_on_empty_collection_returns_empty_list(store):
    assert store.find("nothing", lambda d: True) == []


def test_concurrent_puts_do_not_corrupt(store):
    errors = []

    def writer(i):
        try:
            for j in range(20):
                store.put("bench", f"w{i}", {"count": j})
        except Exception as exc:  # pragma: no cover - 记录线程内失败
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(store.find("bench", lambda d: True)) == 5
