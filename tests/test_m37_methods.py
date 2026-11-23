# -*- coding: utf-8 -*-
"""M37: 方法策展——人设/目标为核、主人提示是素材不是上限、自主发明方法。"""
import json

import pytest

from proto.profile.methods import (BUILTIN_METHODS, MethodLibrary,
                                   detect_restriction)


class MethodLLM:
    """脚本化发明调用。"""
    def __init__(self, reply):
        self.reply = reply
        self.calls = []
    def complete(self, system, user):
        self.calls.append(system + user)
        return self.reply


@pytest.fixture()
def lib(tmp_path):
    from proto.state.store import FileSystemStore
    return MethodLibrary(FileSystemStore(tmp_path))


class TestLibrary:
    def test_builtin_seed_persisted(self, lib, tmp_path):
        from proto.state.store import FileSystemStore
        assert len(lib.methods) == len(BUILTIN_METHODS)
        again = MethodLibrary(FileSystemStore(tmp_path))   # 重启恢复
        assert again.names() == lib.names()

    def test_add_dedupes_by_name(self, lib):
        assert lib.add("github_scan", "重复名", "self_invented") is None
        assert lib.add("new_one", "全新做法", "self_invented") is not None
        assert len([m for m in lib.methods if m.name == "new_one"]) == 1

    def test_invent_adds_hinted_and_invented(self, lib):
        reply = json.dumps({
            "owner_hinted": [{"name": "先看苹果 HIG 文档",
                              "description": "抓 developer.apple.com 设计规范精读"}],
            "invented": [{"name": "widget_poc_bench",
                          "description": "写最小 widget 原型并用 run_python 测刷新耗时"}]})
        llm = MethodLLM(reply)
        added = lib.invent(llm, lambda c, s, u: c.complete(s, u),
                           "iOS 专家", "做待办 widget", context="试试先读文档")
        names = [m.name for m in added]
        assert "先看苹果_hig_文档" in names or any("hig" in n for n in names)
        assert "widget_poc_bench" in names
        assert any(m.source == "owner_hint" for m in added)
        assert any(m.source == "self_invented" for m in added)
        # 同名再发明 → 新颖性去重
        assert lib.invent(llm, lambda c, s, u: c.complete(s, u),
                          "iOS 专家", "另一个目标") == []

    def test_invent_offline_skips(self, lib):
        assert lib.invent(None, lambda c, s, u: "", "p", "g") == []
        assert lib.last_invent_ts == 0.0

    def test_menu_guarantees_invented_method(self, lib):
        lib.add("fancy_new_way", "贴目标的新做法", "self_invented")
        # 把内置方法刷成高频高胜率，挤出菜单
        for m in lib.methods:
            if m.source == "builtin":
                m.used, m.wins = 50, 49
        menu = lib.menu("iOS 专家", "做待办 widget")
        assert any(m.name == "fancy_new_way" for m in menu), \
            "菜单必须保底包含自发明方法（想法设法是默认值）"

    def test_menu_block_marks_sources(self, lib):
        lib.add("hint_way", "主人提示的做法", "owner_hint")
        lib.add("my_way", "我发明的做法", "self_invented")
        block = lib.menu_block("iOS 专家", "做待办 widget")
        assert "[可用方法]" in block
        assert "主人提示" in block and "我的发明★" in block
        assert "而非边界" in block                    # 明示素材非上限


class TestRestriction:
    def test_explicit_restriction_detected(self):
        assert detect_restriction("只能用 benchmark_compare 方法做选型") \
            == "benchmark_compare 方法做选型"
        assert detect_restriction("仅限读官方文档") is not None

    def test_suggestion_is_not_restriction(self):
        assert detect_restriction("可以参考 GitHub 项目，多读文档") is None
        assert detect_restriction("") is None

    def test_restriction_filters_menu_without_deadend(self, lib):
        lib.set_restriction("只能用 benchmark_compare")
        menu = lib.menu("x", "选型")
        assert all("benchmark" in f"{m.name}{m.description}" or "对比" in m.description
                   for m in menu) or len(menu) == len(lib.methods)
        # 限定到不存在的方法 → 不死锁：回退全量并标注
        lib.set_restriction("只能用 不存在的方法xyz")
        assert lib.menu("x", "y")          # 不抛错、不空菜单
        assert "[主人限定]" in lib.menu_block("x", "y")


class TestAccounting:
    def test_usage_and_outcome_change_ranking(self, lib):
        lib.add("winning_way", "通用做法", "self_invented")
        lib.add("losing_way", "通用做法", "self_invented")
        for _ in range(5):
            lib.mark_used(["winning_way", "losing_way"])
        lib.mark_outcome(["winning_way"], True)
        # winning 胜率高应排前（两者相关性与新颖性相同）
        menu = lib.menu("p", "g", k=20)
        assert menu.index(next(m for m in lib.methods if m.name == "winning_way")) \
            < menu.index(next(m for m in lib.methods if m.name == "losing_way"))


class TestWorkOnGoalIntegration:
    def test_prompt_carries_method_menu_and_invention_audited(self, tmp_path):
        from proto.tools.webtools import WebToolkit
        from proto.llm.client import LLMClient
        from proto.runtime.agent import ProtoAgent

        class Scripted(LLMClient):
            def __init__(self):
                self.prompts = []
            def complete(self, system, user):
                self.prompts.append(system + "|" + user)
                if "独立验收员" in system:            # 质量门：给合法通过判定
                    return '{"verdict": "pass", "missing": []}'
                if "owner_hinted" in system:          # 发明调用
                    return json.dumps({
                        "owner_hinted": [],
                        "invented": [{"name": "read_wwdc_notes",
                                      "description": "抓 WWDC session 笔记站精读并摘录"}]})
                if "验收" in system or "acceptance" in system.lower():
                    return '{"acceptance": "项目目录有产物文件"}'
                if "一句话制定执行计划" in system:
                    return "读资料写文件"
                if "任务执行循环" in system:
                    self.loop_turn = getattr(self, "loop_turn", 0) + 1
                    if self.loop_turn == 1:
                        return json.dumps({"tool": "write_file",
                                           "args": {"path": "projects/widget/notes.md",
                                                    "content": "read_wwdc_notes 摘录"},
                                           "category": "workspace_write"})
                    return json.dumps({"done": True, "summary": "read_wwdc_notes 完成"})
                return "好的。"

        llm = Scripted()
        agent = ProtoAgent(data_dir=tmp_path, llm=llm, goal_interval_s=10 ** 9,
                           webtools=WebToolkit(
                               fetch=lambda u, x="t": (_ for _ in ()).throw(OSError("禁网"))))
        node = agent.goal_tree.add("调研 widget 最佳实践", level="mid",
                                   acceptance="项目目录有产物文件")
        agent._curate_methods(node.text, pool="self_initiated")
        result = agent.work_on_goal(node)
        # 任务提示词里带方法菜单 + 自发明方法名
        task_prompts = [p for p in llm.prompts if "调研 widget 最佳实践" in p]
        assert any("[可用方法]" in p and "read_wwdc_notes" in p for p in task_prompts)
        # 发明入审计；战果归因记账（摘要里提到方法名）
        assert agent.spine.find(lambda d: d["event_type"] == "method_invent")
        assert sum("owner_hinted" in prompt for prompt in llm.prompts) == 1
        assert agent.spine.find(lambda d: d["event_type"] == "method_used")
        assert agent.methods_lib.methods[-1].used >= 1
        assert result.get("status") in ("completed", "verified")

    def test_owner_restriction_flows_from_priority(self, tmp_path):
        from proto.tools.webtools import WebToolkit
        from proto.llm.client import LLMClient
        from proto.runtime.agent import ProtoAgent

        class Null(LLMClient):
            def complete(self, system, user):
                return '{"acceptance": "有产出"}' if "acceptance" in system.lower() else "好的。"

        agent = ProtoAgent(data_dir=tmp_path, llm=Null(), goal_interval_s=10 ** 9,
                           webtools=WebToolkit(
                               fetch=lambda u, x="t": (_ for _ in ()).throw(OSError("禁网"))))
        r = agent.set_priority("上架验证只能用 benchmark_compare，不要用其他方法")
        assert r["method_restriction"] is not None
        assert agent.methods_lib.restriction
        assert agent.spine.find(
            lambda d: d["event_type"] == "owner_override"
            and d["payload"].get("action") == "method_restriction")
        # 普通提示不构成限定
        r2 = agent.set_priority("参考 GitHub 项目做调研，方法不限")
        assert r2["method_restriction"] is None       # 已清空（新文本无限定）
        assert agent.methods_lib.restriction is None
