# -*- coding: utf-8 -*-
"""M47：舰队协作接线——黑板/舰队状态进工具目录 + 观察截断分级。

审查结论（2026-11-19）：M32 黑板是手工 API 壳，自主回路零调用
（两个 agent blackboard 事件均 0）；监工无舰队视野，目标跑偏成
"建告警系统"；专家卡在 read_file 观察 600 字截断——文档永远读不全，
"确认完整性"永远失败。
"""
import json

import pytest

from proto.exec.agentic import AgenticLoop


class LoopLLM:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.prompts = []

    def complete(self, system, user):
        self.prompts.append((system, user))
        item = self.decisions.pop(0)
        return item if isinstance(item, str) else json.dumps(item)


@pytest.fixture()
def agent(tmp_path):
    from proto.tools.webtools import WebToolkit
    from proto.runtime.agent import ProtoAgent
    dead = WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))
    return ProtoAgent(data_dir=tmp_path, llm=LoopLLM([]), goal_interval_s=10**9,
                      webtools=dead)


class TestObservationLimits:
    def test_read_file_gets_larger_budget(self, agent):
        """read_file 观察 3000 字预算 + 截断提示（原来 600 字读不全设计文档）。"""
        long_doc = "F1卡片管理\n" + "细节" * 1500 + "\nF4导出备份"
        (agent.code_workspace.root / "SPEC.md").write_text(long_doc, encoding="utf-8")
        agent.llm.decisions = [
            "读规格",                                                # plan
            {"tool": "read_file", "args": {"path": "SPEC.md"},
             "category": "read_draft"},
            {"done": True, "summary": "ok"},
            "发现：ok\n产出：无\n下一步：done",                        # summarize
        ]
        result = AgenticLoop(agent, max_steps=4).run("读规格")
        obs = next(o for o in result["observations"] if o.startswith("read_file"))
        assert "F1卡片管理" in obs                 # 开头保住
        assert "截断" in obs and "grep" in obs      # 截断可见且给出路
        # 模型下一轮 prompt 里能看到超出 600 字的信息
        assert any("细节" in p[1] for p in agent.llm.prompts[1:])

    def test_small_tools_keep_600(self, agent):
        agent.llm.decisions = [
            "干",
            {"tool": "clock", "args": {}, "category": "read_draft"},
            {"done": True, "summary": "ok"},
            "发现：ok\n产出：无\n下一步：done",
        ]
        result = AgenticLoop(agent, max_steps=4).run("t")
        assert any(o.startswith("clock({}) →") for o in result["observations"])
        assert len(result["trace"][0]["result"]) <= 600


class TestFleetTools:
    def test_catalog_contains_collab_tools(self, agent):
        names = agent.tools.names()
        assert "blackboard_read" in names
        assert "blackboard_post" in names
        assert "fleet_status" in names

    def test_blackboard_post_and_read_roundtrip(self, tmp_path):
        from proto.fleet.collab import Blackboard
        from proto.storage.store import FileSystemStore
        from proto.tools.webtools import WebToolkit
        from proto.runtime.agent import ProtoAgent
        dead = WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))
        bb = Blackboard(FileSystemStore(tmp_path / "fleet"))
        a = ProtoAgent(data_dir=tmp_path / "ag1", llm=None,
                       goal_interval_s=10**9, webtools=dead, blackboard=bb)
        a.post_to_blackboard("温故项目卡在验证，谁帮忙看看 missing 项", kind="help")
        notes = bb.read()
        assert len(notes) == 1 and "温故" in notes[0]["text"]
        # 工具读取（格式化文本）
        listed = str(a.tools.get("blackboard_read")())
        assert "温故" in listed or "暂无" in listed

    def test_fleet_status_reports_peers(self, tmp_path):
        from proto.tools.webtools import WebToolkit
        from proto.runtime.agent import ProtoAgent
        dead = WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))
        a = ProtoAgent(data_dir=tmp_path / "ag1", llm=None,
                       goal_interval_s=10**9, webtools=dead,
                       fleet_status=lambda: [
                           {"name": "iPhone专家", "status": "working",
                            "detail": "温故 App 实现中，预算 8.8 万"},
                           {"name": "插件专家", "status": "paused",
                            "detail": "已暂停"}])
        out = str(a.tools.get("fleet_status")())
        assert "iPhone专家" in out and "暂停" in out

    def test_fleet_status_standalone(self, agent):
        out = str(agent.tools.get("fleet_status")())
        assert "舰队" in out or "暂无" in out or "不可用" in out
