# -*- coding: utf-8 -*-
"""M78 插件化：ToolPack 装配必须逐值复刻原硬编码装配（对照测试先行钉死现值）。

这三组值原先散在 agentic.READ_ONLY_TOOLS / agentic.OBS_LIMITS / agent._tool_io，
重构为注册表元数据后，任何漂移都会在这里失败。
"""
import pytest

from proto.runtime.agent import ProtoAgent

# 与重构前 agent.py 装配 + agentic.py 清单逐值对照（2026-11-23）
BASE_NAMES = {
    "clock", "scan_style", "polish_style",
    "read_file", "read_files", "write_file", "edit_file", "list_dir", "grep",
    "search_symbols", "explore_code", "read_skill", "inspect_build",
    "run_python", "run_command",
    "web_fetch", "github_search", "github_readme",
    "blackboard_read", "blackboard_post", "fleet_status",
    "company_context", "company_inbox", "company_send",
}
READ_ONLY_EXPECTED = {
    "list_dir", "read_file", "read_files", "grep", "search_symbols",
    "explore_code", "clock", "web_fetch", "github_search", "github_readme",
    "read_skill", "inspect_build", "scan_style", "polish_style",
}
OBS_EXPECTED = {
    "read_file": 3000, "read_files": 6000, "run_python": 1500,
    "run_command": 4000, "grep": 1000, "read_skill": 2500,
    "inspect_build": 3600, "search_symbols": 3200, "explore_code": 2400,
    "scan_style": 4000, "polish_style": 5200, "blackboard_read": 1200,
    "fleet_status": 1200, "company_context": 3700, "company_inbox": 3300,
    "github_readme": 3000, "web_fetch": 3000,
}
IO_EXPECTED = {
    "web_fetch", "github_search", "github_readme", "run_python", "run_command",
    "list_dir", "read_file", "read_files", "grep", "write_file", "edit_file",
    "delegate_coding", "inspect_build", "search_symbols", "explore_code",
}


@pytest.fixture()
def agent(tmp_path):
    return ProtoAgent(data_dir=tmp_path)


class TestAssemblyPinsLegacy:
    def test_registered_names(self, agent):
        expected = set(BASE_NAMES)
        if getattr(agent, "bridge", None) and agent.bridge.available:
            expected.add("delegate_coding")
        assert set(agent.tools.names()) == expected

    def test_read_only_tools(self, agent):
        assert agent.tools.read_only_tools() == READ_ONLY_EXPECTED

    def test_obs_limits(self, agent):
        for name, limit in OBS_EXPECTED.items():
            assert agent.tools.obs_limit(name) == limit, name

    def test_obs_default_for_undeclared(self, agent):
        assert agent.tools.obs_limit("github_search") == 600
        assert agent.tools.obs_limit("blackboard_post") == 600

    def test_background_io_tools(self, agent):
        expected = set(IO_EXPECTED)
        if not (getattr(agent, "bridge", None) and agent.bridge.available):
            expected.discard("delegate_coding")
        assert {t for t in agent.tools.names() if agent.tools.is_background_io(t)} == expected

    def test_static_categories(self, agent):
        assert agent.tools.policy("write_file").category.name == "WORKSPACE_WRITE"
        assert agent.tools.policy("web_fetch").category.name == "READ_DRAFT"
        assert agent.tools.policy("blackboard_post").category.name == "READ_DRAFT"

    def test_sandboxed_run_tools_pairing(self, agent):
        """run_python/run_command：未隔离后端必须 owner_only，隔离后端反之。"""
        for name in ("run_python", "run_command"):
            category = agent.tools.policy(name).category.name
            owner_only = agent.tools.policy(name).owner_only
            assert category in ("WORKSPACE_WRITE", "VISIBLE_WRITE")
            assert owner_only == (category == "VISIBLE_WRITE"), name

    def test_definitions_cover_all_tools(self, agent):
        defs = agent.tools.definitions()
        assert {d["name"] for d in defs} == set(agent.tools.names())


class TestToolSpec:
    def test_register_into_records_metadata(self):
        from proto.exec.router import ToolRegistry
        from proto.plugins.base import ToolSpec

        registry = ToolRegistry()
        spec = ToolSpec(name="probe", fn=lambda: "ok", category="READ_DRAFT",
                        description="探针", read_only=True, obs_limit=123,
                        background_io=True, parameters={"type": "object"})
        spec.register_into(registry)
        assert registry.names() == {"probe"}
        assert registry.read_only_tools() == {"probe"}
        assert registry.obs_limit("probe") == 123
        assert registry.is_background_io("probe")
        assert registry.definitions()[0]["parameters"] == {"type": "object"}

    def test_parameters_fallback_to_builtin(self):
        from proto.exec.router import ToolRegistry
        from proto.plugins.base import ToolSpec

        registry = ToolRegistry()
        ToolSpec(name="clock", fn=lambda: "t", category="READ_DRAFT").register_into(registry)
        assert registry.definitions()[0]["parameters"] is not None

    def test_pack_bridge_empty_without_bridge(self):
        from proto.plugins import pack_bridge

        class _NoBridge:
            bridge = None

        assert pack_bridge.specs(_NoBridge()) == []
