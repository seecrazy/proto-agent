# -*- coding: utf-8 -*-
"""M31: 生命周期与规矩管理——暂停恢复语义/瞬态持久化/规矩编辑删除/项目目录控制台可配。"""
import json
import time

import pytest

from proto.llm.client import LLMClient


class Null(LLMClient):
    def complete(self, s, u):
        return "[]" if "归因" in s else "好的。"


def dead_web():
    from proto.tools.webtools import WebToolkit
    return WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))


@pytest.fixture()
def agent(tmp_path):
    from proto.runtime.agent import ProtoAgent
    return ProtoAgent(data_dir=tmp_path, llm=Null(), goal_interval_s=10 ** 9,
                      webtools=dead_web())


class TestTransientPersistence:
    def test_praise_times_and_cooldown_survive_restart(self, tmp_path):
        from proto.runtime.agent import ProtoAgent
        a1 = ProtoAgent(data_dir=tmp_path, llm=Null(), goal_interval_s=10 ** 9,
                        webtools=dead_web())
        a1._praise_times = [111.0, 222.0]
        a1._last_self_act = 999.0
        a1._save_transient()
        a2 = ProtoAgent(data_dir=tmp_path, llm=Null(), goal_interval_s=10 ** 9,
                        webtools=dead_web())
        assert a2._praise_times == [111.0, 222.0]
        assert a2._last_self_act == 999.0

    def test_chat_updates_transient(self, agent):
        import time as _t
        agent._praise_times.append(_t.time())      # 近期时间戳（3h 窗口内）
        agent.chat("你好")
        from proto.runtime.agent import ProtoAgent
        a2 = ProtoAgent(data_dir=agent.data_dir, llm=Null(), goal_interval_s=10 ** 9,
                        webtools=dead_web())
        assert len(a2._praise_times) >= 1


class TestRuleCRUD:
    def test_edit_rule(self, agent):
        rid = agent.instruct("旧规矩文本")
        agent.edit_rule(rid, "新规矩文本")
        assert agent.ledger.get(rid)["text"] == "新规矩文本"
        assert agent.spine.find(lambda d: d["event_type"] == "rule_edit")

    def test_delete_rule(self, agent):
        rid = agent.instruct("要删的规矩")
        agent.delete_rule(rid)
        assert agent.ledger.get(rid) is None
        assert agent.spine.find(lambda d: d["event_type"] == "rule_delete")

    def test_delete_nonexistent_raises(self, agent):
        with pytest.raises(KeyError):
            agent.delete_rule("nope")


class TestProjectsRootConfig:
    def test_projects_root_stored_and_reloadable(self, agent, tmp_path):
        agent.set_projects_root(str(tmp_path / "consoledir"))
        assert "PROTO_PROJECTS_ROOT" not in __import__("os").environ  # 不污染进程环境
        from proto.runtime.agent import ProtoAgent
        a2 = ProtoAgent(data_dir=agent.data_dir, goal_interval_s=10 ** 9,
                        webtools=dead_web())
        assert str(a2.code_workspace.root).startswith(str(tmp_path / "consoledir"))

    def test_invalid_path_rejected(self, agent):
        with pytest.raises(ValueError):
            agent.set_projects_root("/nonexistent-root-xyz/abc")
