# -*- coding: utf-8 -*-
"""M53：项目目录 ASCII 化 + goal→dir 持久映射（用户第二次点名中文目录名）。

M41 只修了长度没修字符集（当时的判断错误，本次根治）：
- slug 只取 ASCII 单词（swiftui-app），纯中文目标回退 goal-<id8>
- goal→dir 映射持久化：slug 算法再变，既有目标不换目录
- 首次映射时若存在旧式中文目录则原地改名（保住已有进度）
"""
import pytest


@pytest.fixture()
def agent(tmp_path):
    from proto.exec.webtools import WebToolkit
    from proto.runtime.agent import ProtoAgent
    dead = WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))
    return ProtoAgent(data_dir=tmp_path, llm=None, goal_interval_s=10**9,
                      webtools=dead)


class TestAsciiSlug:
    def test_chinese_goal_yields_ascii_slug(self):
        from proto.runtime.agent import ProtoAgent
        slug = ProtoAgent._goal_slug(
            "以 SwiftUI 完整实现 app：@main App 入口、数据模型与本地持久化")
        assert slug.startswith("swiftui-app")
        assert all(c in "-0123456789abcdefghijklmnopqrstuvwxyz"
                   for c in slug)

    def test_pure_chinese_falls_back_to_id(self):
        from proto.runtime.agent import ProtoAgent
        assert ProtoAgent._goal_slug("确定应用概念与设计规格", "ab12cd34") \
            == "goal-ab12cd34"

    def test_length_capped(self):
        from proto.runtime.agent import ProtoAgent
        slug = ProtoAgent._goal_slug("build a very long project name here")
        assert len(slug) <= 24


class TestGoalDirMapping:
    def test_mapping_persists_across_slug_change(self, agent):
        """同一目标两次推进拿到同一目录（即使 slug 算法再变也不换目录）。"""
        from proto.goals.system import GoalNode
        node = GoalNode(id="g123", level="mid", text="以 SwiftUI 完整实现 app",
                        acceptance="x")
        agent.goal_tree.add(node.text, "mid")   # 占位：树里有同文本目标
        d1 = agent._project_dir_for(node)
        d2 = agent._project_dir_for(node)
        assert d1 == d2 == "projects/swiftui-app"
        # 篡改 slug 算法输出（模拟未来变更）：映射仍稳定（测完恢复类方法）
        from proto.runtime.agent import ProtoAgent
        orig = ProtoAgent.__dict__["_goal_slug"]   # 原始 staticmethod 包装
        agent.__class__._goal_slug = staticmethod(
            lambda text, fid="": "changed-" + fid)
        try:
            assert agent._project_dir_for(node) == "projects/swiftui-app"
        finally:
            agent.__class__._goal_slug = orig

    def test_legacy_chinese_dir_migrated(self, agent):
        from proto.runtime.agent import ProtoAgent  # noqa: F401  (恢复用)
        """已有中文目录的目标首次映射时原地改名（保进度）。"""
        from proto.goals.system import GoalNode
        legacy = agent.code_workspace.root / "projects" / "以-SwiftUI-完整实现-app"
        legacy.mkdir(parents=True)
        (legacy / "WenguCore.swift").write_text("x", encoding="utf-8")
        node = GoalNode(id="g9", level="mid", text="以 SwiftUI 完整实现 app",
                        acceptance="x")
        d = agent._project_dir_for(node)
        assert d == "projects/swiftui-app"
        new = agent.code_workspace.root / d
        assert (new / "WenguCore.swift").exists()      # 文件跟过来了
        assert not legacy.exists()

    def test_drop_uses_mapped_dir(self, agent):
        from proto.goals.system import GoalNode
        node = agent.goal_tree.add("以 SwiftUI 实现温故 app", "mid")
        d = agent._project_dir_for(node)
        real = agent.code_workspace.root / d
        real.mkdir(parents=True)
        (real / "a.swift").write_text("x", encoding="utf-8")
        agent.drop_goal(node.id)
        archived = agent.code_workspace.root / "projects" / "_archived" / real.name
        assert (archived / "a.swift").exists()
