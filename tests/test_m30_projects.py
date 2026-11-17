# -*- coding: utf-8 -*-
"""M30: 项目工位——自主开发的落盘目录、可配置、按 agent 隔离。"""
import json

import pytest

from proto.llm.client import LLMClient


class Dev(LLMClient):
    """脚本桩：循环决策从队列取；write_file 的 path 若为 __PROJECT__
    则替换为任务文本中指定的项目目录。"""
    def __init__(self):
        self.decisions = []
        self.last_task = ""
    def complete(self, system, user):
        if "归因" in system:
            return "[]"
        if "最小可检验定义" in system:
            return json.dumps({"acceptance": "目录中存在可运行代码"})
        if "独立验收" in system:
            return json.dumps({"verdict": "pass"})
        if "分解" in system:
            return json.dumps([{"text": "插件脚手架", "acceptance": "目录存在"}])
        if "预测" in system:
            return json.dumps([])
        if "晨间" in system or "简报" in system:
            return "简报"
        if "任务执行循环" in system:
            self.last_task = user
            import re as _re
            m = _re.search(r"projects/[^/\s）)]+", user)
            proj = m.group(0) if m else "projects/x"
            if self.decisions:
                item = self.decisions.pop(0)
                return json.dumps(item).replace("__PROJECT__", proj)
            return json.dumps({"done": True, "summary": "完成"})
        return "好的。"


def dead_web():
    from proto.exec.webtools import WebToolkit
    return WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))


@pytest.fixture()
def agent(tmp_path):
    from proto.runtime.agent import ProtoAgent
    llm = Dev()
    llm.decisions = [
        {"tool": "write_file", "args": {"path": "__PROJECT__/main.py",
                                        "content": "print('proto')"},
         "category": "workspace_write"},
    ]
    return ProtoAgent(data_dir=tmp_path, llm=llm, goal_interval_s=10 ** 9,
                      webtools=dead_web())


class TestProjectWorkspace:
    def test_goal_execution_writes_into_project_dir(self, agent):
        """目标牵引执行 → 文件落在 projects/<目标slug>/ 而非工作区根。"""
        agent.set_priority("开发浏览器插件")
        node = agent.goal_tree.select_executable("开发浏览器插件")
        result = agent.work_on_goal(node)
        assert result["status"] in ("verified", "completed")
        files = [str(p.relative_to(agent.code_workspace.root))
                 for p in agent.code_workspace.root.rglob("*") if p.is_file()]
        assert files and all(f.startswith("projects/") for f in files), files

    def test_projects_root_configurable_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROTO_PROJECTS_ROOT", str(tmp_path / "mydev"))
        from proto.runtime.agent import ProtoAgent
        a = ProtoAgent(data_dir=tmp_path / "agent", goal_interval_s=10 ** 9,
                       webtools=dead_web())
        assert str(a.code_workspace.root).startswith(str(tmp_path / "mydev"))

    def test_project_dir_named_from_goal_slug(self, agent):
        from proto.runtime.agent import ProtoAgent
        slug = ProtoAgent._goal_slug("开发浏览器插件: 广告过滤 MVP!")
        assert slug == "开发浏览器插件-广告过滤-MVP"

    def test_per_agent_isolation(self, tmp_path):
        from proto.runtime.agent import ProtoAgent
        a1 = ProtoAgent(data_dir=tmp_path / "a1", goal_interval_s=10 ** 9,
                        webtools=dead_web())
        a2 = ProtoAgent(data_dir=tmp_path / "a2", goal_interval_s=10 ** 9,
                        webtools=dead_web())
        assert a1.code_workspace.root != a2.code_workspace.root
