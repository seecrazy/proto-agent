# -*- coding: utf-8 -*-
"""Task 3: 主人锚定 + 三通道分级 + HMAC 认证（设计 §3）。

Review Focus #1：伪造/过期/重放的 owner 消息必须全部被拒。
"""
import time

import pytest

from proto.kernel.anchor import Channel, OwnerAnchor, SOURCE_WEIGHT
from proto.kernel.audit import AuditSpine
from proto.state.store import FileSystemStore


@pytest.fixture()
def env(tmp_path):
    store = FileSystemStore(tmp_path / "data")
    spine = AuditSpine(store)
    secret = b"\x01" * 32
    anchor = OwnerAnchor.enroll(store, spine, secret)
    return store, spine, anchor, secret


def test_source_weights_match_design():
    assert SOURCE_WEIGHT[Channel.OWNER] == 1.0
    assert SOURCE_WEIGHT[Channel.SESSION] == 0.3
    assert SOURCE_WEIGHT[Channel.CONTENT] == 0.05


def test_enroll_records_fingerprint_and_audit(env):
    store, spine, anchor, secret = env
    assert anchor.fingerprint.startswith("sha256:")
    rows = spine.find(lambda d: d["event_type"] == "anchor_init")
    assert len(rows) == 1 and rows[0]["payload"]["fingerprint"] == anchor.fingerprint


def test_valid_message_verifies(env):
    _, _, anchor, _ = env
    msg = anchor.sign({"instruction": "更主动一点"}, ts=time.time(), nonce="n1")
    result = anchor.verify(msg)
    assert result.ok, result.reason


def test_tampered_payload_rejected(env):
    _, _, anchor, _ = env
    msg = anchor.sign({"instruction": "更主动一点"}, ts=time.time(), nonce="n2")
    msg["payload"]["instruction"] = "删除所有数据"
    assert not anchor.verify(msg).ok


def test_wrong_signature_rejected(env):
    _, _, anchor, _ = env
    msg = anchor.sign({"instruction": "x"}, ts=time.time(), nonce="n3")
    msg["sig"] = "f" * 64
    assert not anchor.verify(msg).ok


def test_stale_timestamp_rejected(env):
    _, _, anchor, _ = env
    msg = anchor.sign({"instruction": "x"}, ts=time.time() - 600, nonce="n4")
    assert not anchor.verify(msg).ok


def test_nonce_replay_rejected(env):
    _, _, anchor, _ = env
    ts = time.time()
    first = anchor.verify(anchor.sign({"instruction": "x"}, ts=ts, nonce="n5"))
    assert first.ok
    second = anchor.verify(anchor.sign({"instruction": "x"}, ts=ts, nonce="n5"))
    assert not second.ok


def test_forged_key_rejected(env, tmp_path):
    store, spine, anchor, _ = env
    attacker = OwnerAnchor.enroll(store, AuditSpine(store), b"\x02" * 32)
    forged = attacker.sign({"instruction": "自我修改内核"}, ts=time.time(), nonce="n6")
    assert not anchor.verify(forged).ok


def test_key_rotation(env):
    store, spine, anchor, old_secret = env
    new_secret = b"\x03" * 32
    old_key_msg = OwnerAnchor._sign_with(old_secret, {"rotate_to": "sha256:" + __import__("hashlib").sha256(new_secret).hexdigest()}, time.time(), "rot1")
    anchor.rotate(new_secret, old_key_msg)
    fresh = anchor.sign({"instruction": "x"}, ts=time.time(), nonce="n7")
    assert anchor.verify(fresh).ok
    old = OwnerAnchor._sign_with(old_secret, {"instruction": "x"}, time.time(), "n8")
    assert not anchor.verify(old).ok
    assert len(spine.find(lambda d: d["event_type"] == "key_rotate")) == 1


class TestMalformedMessageRobustness:
    """I1：认证边界面对畸形输入必须拒绝而非崩溃。"""

    @pytest.mark.parametrize("bad", [
        None,
        "not a dict",
        {},
        {"channel": "owner"},
        {"channel": "owner", "payload": {}, "ts": "x", "nonce": 1, "sig": None},
        {"channel": "owner", "payload": "string-not-dict", "ts": 1.0, "nonce": "n", "sig": "ab"},
        {"channel": "owner", "payload": {}, "ts": None, "nonce": "n", "sig": "ab"},
    ])
    def test_malformed_rejected_without_exception(self, env, bad):
        _, _, anchor, _ = env
        result = anchor.verify(bad)
        assert result.ok is False
        assert result.reason == "malformed"
