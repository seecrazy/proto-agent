# -*- coding: utf-8 -*-
"""M41: 专家工作法机制——轮转防饿死 / 交付阶梯 / 工作区治理 / 编译检查。"""
import json

import pytest

from proto.exec.webtools import WebToolkit
from proto.llm.client import LLMClient
from proto.runtime.agent import ProtoAgent


class Null(LLMClient):
    def complete(self, system, user):
        return "（离线）"


@pytest.fixture()
def agent(tmp_path):
    return ProtoAgent(data_dir=tmp_path / "d", llm=Null(), goal_interval_s=10 ** 9,
                      webtools=WebToolkit(
                          fetch=lambda u, x="t": (_ for _ in ()).throw(OSError("禁网"))))


class TestFailStreakRotation:
    def test_failing_goal_rotates_selection(self, agent):
        """整夜卡死根因：验证反复不过的目标独占选择器 → 轮转到兄弟目标。"""
        design = agent.goal_tree.add("设计规格甲", level="mid",
                                     acceptance="有设计文档")
        impl = agent.goal_tree.add("功能实现乙", level="mid",
                                   acceptance="有实现代码")
        assert agent.goal_tree.select_executable("").id == design.id
        design.fail_streak = 2
        agent.goal_tree._store.put("goaltree", design.id, design.to_dict())
        picked = agent.goal_tree.select_executable("")
        assert picked.id == impl.id              # 失败 streak 大者让位

    def test_fail_streak_persisted_across_restart(self, agent, tmp_path):
        n = agent.goal_tree.add("验证总不过", level="mid", acceptance="有文档")
        n.fail_streak = 3
        agent.goal_tree._store.put("goaltree", n.id, n.to_dict())
        a2 = ProtoAgent(data_dir=tmp_path / "d", llm=Null(), goal_interval_s=10 ** 9,
                        webtools=WebToolkit(
                            fetch=lambda u, x="t": (_ for _ in ()).throw(OSError("禁网"))))
        assert a2.goal_tree.get(n.id).fail_streak == 3


class TestDeliveryPipeline:
    def test_decompose_prompt_carries_ladder(self):
        from proto.goals.system import DECOMPOSE_PROMPT
        assert "交付阶梯" in DECOMPOSE_PROMPT
        assert "就绪交付" in DECOMPOSE_PROMPT
        assert "待主人提供" in DECOMPOSE_PROMPT

    def test_acceptance_prompt_caps_evidence(self):
        from proto.quality.gate import ACCEPTANCE_PROMPT
        assert "3 条以内" in ACCEPTANCE_PROMPT

    def test_local_build_check_builtin(self, agent):
        names = [m.name for m in agent.methods_lib.methods]
        assert "local_build_check" in names
        assert any("swiftc" in m.description for m in agent.methods_lib.methods)

    def test_slug_capped_short(self):
        long_name = ProtoAgent._goal_slug(
            "确定-app-概念与设计规格-选定名称与定位-解决什么问题-目标用户-确认-3-5")
        assert len(long_name) <= 18


class TestWorkspaceGovernance:
    def test_index_written_with_status_and_strays(self, agent):
        root = agent.code_workspace.root / "projects"
        (root / "我的项目").mkdir(parents=True)
        (root / "我的项目" / "a.swift").write_text("let x = 1")
        (root / "loose.py").write_text("print(1)")
        agent.goal_tree.add("我的项目目标完成某功能", level="mid", acceptance="有码")
        agent._refresh_projects_index()
        text = (root / "README.md").read_text(encoding="utf-8")
        assert "我的项目" in text and "进行中" in text
        assert "散落文件" in text and "loose.py" in text

    def test_drop_archives_dead_lineage_keeps_live(self, agent):
        root = agent.code_workspace.root / "projects"
        dead = agent.goal_tree.add("废弃目标某事项", level="mid", acceptance="x")
        live = agent.goal_tree.add("活跃目标另一事项", level="mid", acceptance="y")
        (root / "废弃目标某事项").mkdir(parents=True)
        (root / "活跃目标另一事项").mkdir(parents=True)
        agent.drop_goal(dead.id)
        assert not (root / "废弃目标某事项").exists()
        assert (root / "_archived" / "废弃目标某事项").exists()
        assert (root / "活跃目标另一事项").exists()   # 活谱系不动
