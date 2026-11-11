# -*- coding: utf-8 -*-
"""M9: 无人交互时的生活——心跳/自主草稿/深度回顾/服务端心跳线程。"""
import json
import threading
import time
import urllib.request

import pytest

from proto.llm.client import LLMClient
from proto.runtime.agent import ProtoAgent


class ScriptedLLM(LLMClient):
    def __init__(self, extra=None):
        self.extra = list(extra or [])
        self.drafts = []
    def complete(self, system, user):
        if "归因" in system:
            return "[]"
        if "草稿" in system or "笔记" in system:
            self.drafts.append(user)
            return "## 工作草稿\n围绕主人优先级的三条要点……"
        return self.extra.pop(0) if self.extra else "好的。"


@pytest.fixture()
def agent(tmp_path):
    from proto.exec.webtools import WebToolkit

    def dead_fetch(url, accept="text/html"):
        raise OSError("测试环境禁网")

    return ProtoAgent(data_dir=tmp_path, llm=ScriptedLLM(),
                      goal_interval_s=0,
                      webtools=WebToolkit(fetch=dead_fetch))  # 封闭：每次心跳都到点


class TestIdleLife:
    def test_emotion_decays_on_heartbeat_without_chat(self, agent):
        agent.state.emotion["arousal"] = 0.95
        agent.maybe_proactive()
        assert agent.state.emotion["arousal"] < 0.95

    def test_idle_draft_written_to_scratch_and_proposal_submitted(self, agent):
        agent.state.set_within_bounds("desires.curiosity_gap", 0.9)
        agent.set_priority("补齐支付模块的单元测试")
        message = agent.maybe_proactive()
        # 自主草稿：scratch 里有笔记（read_draft 自主执行，零外部性）
        notes = [p for p in agent.workspace.list_scratch() if p.startswith("notes/")]
        assert notes, agent.workspace.list_scratch()
        # 草稿内容来自 LLM 且带主人优先级上下文
        assert any("支付" in d or "优先级" in d for d in agent.llm.drafts)
        # 晋升提案待批准
        assert "提案" in message and agent.pending_proposals()

    def test_offline_idle_draft_uses_template(self, tmp_path):
        from proto.exec.webtools import WebToolkit
        agent = ProtoAgent(data_dir=tmp_path, goal_interval_s=0,
                           webtools=WebToolkit(fetch=lambda u, accept="text/html":
                                               (_ for _ in ()).throw(OSError("禁网"))))
        agent.state.set_within_bounds("desires.curiosity_gap", 0.9)
        agent.maybe_proactive()
        assert any(p.startswith("notes/") for p in agent.workspace.list_scratch())

    def test_no_desire_over_threshold_no_draft(self, agent):
        # 所有缺口都压到阈值之下
        agent.state.set_within_bounds("desires.curiosity_gap", 0.31)
        agent.state.set_within_bounds("desires.goal_urgency", 0.31)
        assert agent.maybe_proactive() is None
        assert not any(p.startswith("notes/") for p in agent.workspace.list_scratch())


class TestDeepReview:
    def test_daily_review_prunes_and_decays_interest(self, agent):
        for i in range(5):  # 同簇 5 条低效——超出覆盖配额 3 的应被修剪
            agent.library.add(f"低效策略{i}", cluster="junk", utility=0.1)
        for _ in range(6):
            agent.interest.record("webgl", success=True)   # 推到高位
        high = agent.interest.weight("webgl")
        agent._deep_review()
        assert agent.interest.weight("webgl") < high        # 向中性衰减
        junk = [s for s in agent.library.strategies.values() if s.cluster == "junk"]
        assert len(junk) == 3   # 保簇覆盖配额，超额低效被修剪
        assert agent.spine.find(lambda d: d["event_type"] == "deep_review")


class TestServerHeartbeat:
    def test_agent_lives_without_any_page_open(self, tmp_path):
        from proto.web.server import ProtoWebServer

        agent = ProtoAgent(data_dir=tmp_path, llm=ScriptedLLM(),
                           goal_interval_s=0)
        agent.state.set_within_bounds("desires.curiosity_gap", 0.9)
        server = ProtoWebServer(agent, port=0, heartbeat_s=0.1)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            deadline = time.time() + 3
            seen = False
            while time.time() < deadline:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{server.port}/api/proposals")
                req.add_header("X-Proto-Token", server.token)
                with urllib.request.urlopen(req, timeout=2) as resp:
                    if json.loads(resp.read().decode())["proposals"]:
                        seen = True
                        break
                time.sleep(0.1)
            assert seen, "无人打开页面时，服务端心跳应驱动主动提议"
            assert any(p.startswith("notes/")
                       for p in agent.workspace.list_scratch())
        finally:
            server.shutdown()
            server.server_close()
