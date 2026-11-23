# -*- coding: utf-8 -*-
"""M25: 舰队可用性——adopt 幂等/自动去重/活动观测/默认人设。"""
import pytest

from proto.llm.client import LLMClient


class Null(LLMClient):
    def complete(self, s, u):
        return "[]" if "归因" in s else "好的。"


def dead_web():
    from proto.tools.webtools import WebToolkit
    return WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))


@pytest.fixture()
def mgr(tmp_path):
    from proto.runtime.manager import AgentManager
    return AgentManager(root=tmp_path, llm=Null(), webtools=dead_web())


def test_adopt_idempotent_same_dir(mgr, tmp_path):
    from proto.runtime.agent import ProtoAgent
    agent = ProtoAgent(data_dir=tmp_path / "legacy", llm=Null(),
                       goal_interval_s=10 ** 9, webtools=dead_web())
    mgr.adopt(agent, name="默认")
    mgr.adopt(agent, name="默认")          # 重启再收编 → 不应产生重复
    mgr.adopt(agent, name="默认")
    assert len([a for a in mgr.list() if a["name"] == "默认"]) == 1


def test_registry_autodedupe_historical_duplicates(mgr, tmp_path):
    """历史脏数据：同 external_dir 多条 → 加载时自动收敛为一条。"""
    from proto.runtime.agent import ProtoAgent
    agent = ProtoAgent(data_dir=tmp_path / "old", llm=Null(),
                       goal_interval_s=10 ** 9, webtools=dead_web())
    mgr.adopt(agent, name="默认")
    reg = mgr._load_registry()             # 手工制造历史重复
    reg["agents"].append(dict(reg["agents"][0], id="dup1"))
    reg["agents"].append(dict(reg["agents"][0], id="dup2"))
    mgr._save_registry(reg)
    assert len(mgr.list()) == 1


def test_snapshot_includes_activity(mgr):
    info = mgr.create(name="A", persona="软件专家", priority="研究 AI")
    g = mgr.get(info["id"])
    g.spine.append("web_fetch", {"url": "https://github.com/x"})
    row = next(r for r in mgr.list() if r["id"] == info["id"])
    assert row["activity"]["status"]                     # 状态标签
    assert row["last_events"] and "web_fetch" in [e["event_type"] for e in row["last_events"]]


def test_activity_status_labels(mgr):
    from proto.runtime.manager import AgentManager
    activity_of = AgentManager.activity_of
    assert activity_of([{"event_type": "web_fetch"}])["status"] == "学习中"
    assert activity_of([{"event_type": "reflection_write"}])["status"] == "反思中"
    assert activity_of([{"event_type": "prediction_error"}])["status"] == "调查预测落空"
    assert activity_of([{"event_type": "goal_pushed"}])["status"] == "生成目标"
    assert activity_of([{"event_type": "modulation"}])["status"] == "被反馈塑造"
    assert activity_of([])["status"] == "待命"


def test_default_persona_richer():
    from proto.runtime.agent import ProtoAgent
    agent = ProtoAgent(data_dir="/tmp/m25_persona_probe", goal_interval_s=10 ** 9,
                       webtools=dead_web())
    assert len(agent.persona) >= 30        # 不再是四个字的"通用工程助手"
    assert any(k in agent.persona for k in ("验证", "证据", "好奇"))
