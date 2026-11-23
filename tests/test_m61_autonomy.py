# -*- coding: utf-8 -*-
"""M61：GoalForge + 本地验证闭环——自主专家不止写学习笔记。"""
import json
import sys

import pytest

from proto.tools.codetools import CodeTools
from proto.llm.client import LLMClient
from proto.memory.stream import MemoryStream
from proto.quality.gate import artifacts_from_trace
from proto.profile.methods import MethodLibrary
from tests.test_idea_forge import load_example, material_packet


class ExpertLLM(LLMClient):
    def __init__(self):
        self.prompts = []
        self.acceptance_calls = 0
        self.loop_calls = 0
        self.ideation = material_packet(load_example())
        self.ideation["candidates"] = self.ideation["candidates"][:1]

    def complete(self, system, user):
        self.prompts.append((system, user))
        if "GoalForge" in system:
            return json.dumps(self.ideation, ensure_ascii=False)
        if "创意补生成 Agent" in system:
            return "[]"
        if "创意批评 Agent" in system:
            return json.dumps([{"id": "C1", "issues": []}])
        if "目标分解器" in system:
            return json.dumps([
                {"text": "撰写产品规格与界面流", "acceptance": "存在 SPEC.md"},
                {"text": "实现本地交互原型", "acceptance": "存在可运行代码"},
                {"text": "补齐本地测试与编译验证", "acceptance": "测试退出码 0"},
            ], ensure_ascii=False)
        if "已有方法库" in system:
            return json.dumps({"owner_hinted": [], "invented": []})
        if "最小可检验定义" in system:
            self.acceptance_calls += 1
            return json.dumps({"acceptance": "项目目录存在 SPEC.md 且内容非空"})
        if system.startswith("为下面的任务"):
            return "写 SPEC 后验证"
        if "任务执行循环" in system:
            import re
            m = re.search(r"projects/[^/\s）)]+", user)
            project = m.group(0) if m else "projects/fallback"
            self.loop_calls += 1
            if self.loop_calls == 1:
                return json.dumps({
                    "tool": "write_file",
                    "args": {"path": f"{project}/SPEC.md",
                             "content": "# iPhone 记忆卡片\n本地可验证规格\n"},
                    "category": "workspace_write"})
            return json.dumps({"done": True, "summary": "SPEC 已落盘"})
        if "独立验收" in system:
            return json.dumps({"verdict": "pass"})
        if "接力小结" in system:
            return "发现：项目目录明确\n产出：SPEC.md\n下一步：继续下一阶段"
        return "好的。"


def dead_web():
    from proto.tools.webtools import WebToolkit
    return WebToolkit(fetch=lambda u, a="t": (_ for _ in ()).throw(OSError("禁网")))


@pytest.fixture()
def agent(tmp_path):
    from proto.runtime.agent import ProtoAgent
    llm = ExpertLLM()
    a = ProtoAgent(data_dir=tmp_path, llm=llm, goal_interval_s=10 ** 9,
                   webtools=dead_web())
    a.persona = ("iPhone app 软件专家：关注有人愿意用、好用、稳定、可本地验证的 app；"
                 "主动学习优秀产品并完成设计、实现、测试和交付。")
    return a


def test_goal_forge_creates_delivery_ladder_and_executes_locally(agent):
    agent.state.set_within_bounds("desires.curiosity_gap", 0.95)
    result = agent.maybe_proactive()

    longs = agent.goal_tree.active_long_term()
    assert longs and longs[0].source == "self"
    children = [n for n in agent.goal_tree._nodes.values()
                if n.parent_id == longs[0].id]
    assert [c.level for c in children] == ["mid"] * 4
    assert any("交付" in c.text for c in children)
    assert result["status"] == "verified"
    assert any("goal_forge_created" == e["event_type"]
               for e in agent.spine.find(lambda _d: True))


def test_force_act_returns_human_readable_specific_reason(tmp_path):
    from proto.runtime.agent import ProtoAgent
    a = ProtoAgent(data_dir=tmp_path, goal_interval_s=10 ** 9,
                   webtools=dead_web())
    a.state.set_within_bounds("desires.curiosity_gap", 0.9)
    a.kernel.pools.try_consume(
        "self_initiated",
        {"tokens": a.kernel.pools.remaining("self_initiated", "tokens")})
    result = a.force_act()
    assert result["acted"] is False
    assert isinstance(result["message"], str)
    assert "未接 LLM" in result["reason"]


def test_force_act_serializes_work_result_for_ui(agent):
    agent.state.set_within_bounds("desires.curiosity_gap", 0.95)
    result = agent.force_act()
    assert result["acted"] is True
    assert isinstance(result["message"], str)
    assert "目标推进 verified" in result["message"]


def test_generated_acceptance_is_persisted_not_regenerated(agent):
    node = agent.goal_tree.add("自主实现本地工具", "mid", source="self")
    agent.work_on_goal(node)
    first = node.acceptance
    assert first
    calls = agent.llm.acceptance_calls
    agent2 = type(agent)(agent.data_dir, llm=agent.llm,
                         goal_interval_s=10 ** 9, webtools=dead_web())
    node2 = agent2.goal_tree.get(node.id)
    assert node2.acceptance == first
    agent2.work_on_goal(node2)
    assert agent2.llm.acceptance_calls == calls


def test_self_goal_uses_self_budget_floor(agent, tmp_path):
    node = agent.goal_tree.add("自主验证任务", "mid", source="self")
    agent.kernel.pools.try_consume(
        "self_initiated", {"tokens": agent.kernel.pools.remaining(
            "self_initiated", "tokens") - 100})
    result = agent.work_on_goal(node)
    assert result["status"] == "budget_floor_wait"
    assert "self_initiated" in result["reason"]


def test_decompose_preserves_four_ladder_stages(tmp_path):
    from proto.goals.system import GoalTree
    from proto.storage.store import FileSystemStore

    class FourLLM:
        def complete(self, system, user):
            return json.dumps([
                {"text": "设计规格", "acceptance": "设计文件存在"},
                {"text": "功能实现", "acceptance": "实现文件存在"},
                {"text": "本地验证", "acceptance": "测试通过"},
                {"text": "就绪交付", "acceptance": "交付报告存在"},
            ], ensure_ascii=False)

    tree = GoalTree(FileSystemStore(tmp_path))
    node = tree.add("交付一个成熟产品", "long")
    children = tree.decompose(node, FourLLM())
    assert len(children) == 4
    assert "就绪交付" in children[-1].text


class TestLocalVerificationTools:
    def test_run_python_reads_project_workspace(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROTO_EXEC_BACKEND", "subprocess")
        ct = CodeTools(tmp_path / "ws")
        ct.write_file("projects/app/spec.txt", "local artifact")
        r = ct.run_python("print(open('projects/app/spec.txt').read().strip())")
        assert r["exit"] == 0
        assert "local artifact" in r["stdout"]

    def test_run_command_is_array_and_workspace_scoped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROTO_EXEC_BACKEND", "subprocess")
        ct = CodeTools(tmp_path / "ws")
        ct.write_file("projects/app/spec.txt", "command artifact")
        r = ct.run_command(
            [sys.executable, "-c", "print(open('spec.txt').read().strip())"],
            cwd="projects/app")
        assert r["exit"] == 0
        assert "command artifact" in r["stdout"]

    def test_run_command_rejects_shell_and_unknown_tools(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROTO_EXEC_BACKEND", "subprocess")
        ct = CodeTools(tmp_path / "ws")
        assert ct.run_command(["rm", "-rf", "/"])["stderr"] == "command_not_allowed:rm"
        assert ct.run_command(["xcrun", "git", "status"])["stderr"] == \
            "xcrun_tool_not_allowed"

    def test_sandbox_profile_denies_agent_secrets(self, tmp_path):
        protected = tmp_path / "data"
        ct = CodeTools(tmp_path / "ws", protected_paths=[protected])
        profile = ct._sandbox_exec_profile("/tmp/proto-test")
        assert f"(deny file-read* (subpath \"{protected.resolve()}\"))" in profile
        assert "(deny network*)" in profile

    def test_owner_only_tool_cannot_use_auto_quota(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROTO_EXEC_BACKEND", "none")
        monkeypatch.setenv("PROTO_AUTO_APPROVE_N", "1")
        from proto.runtime.agent import ProtoAgent
        from proto.exec.router import Task

        a = ProtoAgent(data_dir=tmp_path, goal_interval_s=10 ** 9,
                       webtools=dead_web())
        result = a.router.run(Task(
            id="elevated", description="未隔离执行", owner_assigned=False,
            steps=[{"tool": "run_command",
                    "args": {"argv": [sys.executable, "-c", "print(1)"]},
                    "category": "visible_write"}]))
        assert result.status == "paused"
        pending = a.pending_proposals()[0]
        assert pending["proposal_id"]
        assert a.auto_quota.remaining() == 1


def test_quality_gate_reads_final_artifact_after_edit(tmp_path):
    ct = CodeTools(tmp_path / "ws")
    ct.write_file("projects/app/README.md", "initial")
    ct.edit_file("projects/app/README.md", "initial", "final verified product")
    trace = [{
        "tool": "edit_file", "args": {
            "path": "projects/app/README.md", "old": "initial",
            "new": "final verified product"}
    }]
    artifacts = artifacts_from_trace(trace, workspace=ct)
    assert "final verified product" in artifacts
    assert "initial" not in artifacts


def test_method_library_rejects_near_duplicate_names():
    lib = MethodLibrary(now=lambda: 1)
    assert lib.add("repo_pattern_mine", "Mine repository patterns for tests",
                   "self_invented") is not None
    assert lib.add("repo_pattern_mining", "Mine repository patterns for tests",
                   "self_invented") is None


def test_memory_retrieval_practices_retrieved_items(tmp_path):
    from proto.storage.store import FileSystemStore
    memory = MemoryStream(FileSystemStore(tmp_path))
    memory.add("fact", "主人喜欢成熟稳定的交付", importance=0.8, ts=100.0)
    before = memory.facts()[0].reinforce_count
    memory.retrieve("主人喜欢什么交付？", now=200.0)
    assert memory.facts()[0].reinforce_count == before + 1


def test_activity_payload_does_not_claim_removed_cooldown():
    from pathlib import Path
    import proto.web.server as server
    source = (Path(server.__file__).read_text(encoding="utf-8"))
    assert '"cooldown_s"' not in source
    assert '"idle_policy": "meaning_gate"' in source
