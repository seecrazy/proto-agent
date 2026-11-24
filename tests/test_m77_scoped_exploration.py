import json
import os

import pytest

from proto.exec.agentic import LOOP_SYSTEM, AgenticLoop
from proto.tools.code_index import CodeIndex
from proto.tools.codetools import CodeTools
from proto.exec.exploration import SYSTEM as EXPLORER_SYSTEM, explore_code, investigate
from proto.exec.contracts import builtin_parameters, checked_arguments
from proto.exec.work_context import decision_input, size
from proto.llm.errors import ModelResponseError
from proto.runtime.agent import ProtoAgent
from proto.tools.webtools import WebToolkit


class ScriptModel:
    def __init__(self, decisions=(), explorer=()):
        self.decisions = list(decisions)
        self.explorer = list(explorer)
        self.actor_inputs = []
        self.explorer_inputs = []

    def complete(self, system, user):
        if system == EXPLORER_SYSTEM:
            self.explorer_inputs.append((system, user))
            return json.dumps(self.explorer.pop(0), ensure_ascii=False)
        if system == LOOP_SYSTEM:
            self.actor_inputs.append((system, user))
            return json.dumps(self.decisions.pop(0), ensure_ascii=False)
        raise AssertionError(system)


class ContextLimitModel(ScriptModel):
    def __init__(self):
        super().__init__([{"done": True, "summary": "已完成"}])
        self.limit_errors = 0

    def complete(self, system, user):
        if system == LOOP_SYSTEM and self.limit_errors == 0:
            self.actor_inputs.append((system, user))
            self.limit_errors += 1
            raise ModelResponseError("model_context_limit", "stream", 400)
        return super().complete(system, user)


class OutputLimitModel(ScriptModel):
    def __init__(self):
        super().__init__([{"done": True, "summary": "已改为小步决策"}])
        self.limit_errors = 0

    def complete(self, system, user):
        if system == LOOP_SYSTEM and self.limit_errors == 0:
            self.actor_inputs.append((system, user))
            self.limit_errors += 1
            raise ModelResponseError("model_output_truncated", "decision")
        return super().complete(system, user)


def make_agent(tmp_path, model=None):
    agent = ProtoAgent(
        data_dir=tmp_path / "data", llm=model or ScriptModel(),
        webtools=WebToolkit(fetch=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline"))),
        goal_interval_s=10**9)
    agent.llm_fast = None
    return agent


def make_project(root):
    project = root / "projects" / "app"
    project.mkdir(parents=True)
    (project / "a_noise.py").write_text("def unrelated():\n    return 1\n", encoding="utf-8")
    source = "\n".join([
        "CONSTANT = 1", "", "class Auth:", "    def verify_access_token(self, token):",
        "        return token == 'AUTH-CHILD-CONTEXT-SECRET'", "", "def helper():",
        "    return Auth()", ""])
    (project / "z_auth.py").write_text(source, encoding="utf-8")
    sibling = root / "projects" / "beta"
    sibling.mkdir(parents=True)
    (sibling / "beta.py").write_text("def verify_access_token():\n    return 'beta'\n", encoding="utf-8")
    return project


def test_symbol_search_locates_nonfirst_file_and_line_window(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    make_project(root)
    index = CodeIndex(root)
    result = index.search("verify access token", cwd="projects/app")
    assert result["hits"][0]["path"] == "projects/app/z_auth.py"
    assert result["hits"][0]["symbol"] == "Auth.verify_access_token"
    hit = result["hits"][0]
    tools = CodeTools(root)
    page = tools.read_file(hit["path"], offset=hit["line"] - 1, limit=4)
    assert "def verify_access_token" in page and "第 4-7 行" in page


def test_symbol_pages_are_cursor_contiguous(tmp_path, monkeypatch):
    monkeypatch.setattr("proto.tools.code_index.PAGE_FILES", 2)
    root = tmp_path / "workspace"
    root.mkdir()
    for name in ("a.py", "b.py", "c.py"):
        (root / name).write_text("def rotate_token():\n    return 1\n", encoding="utf-8")
    index = CodeIndex(root)
    first = index.search("rotate_token", limit=1)
    assert first["next_cursor"] == "b.py"
    assert first["hits"][0]["path"] == "a.py"
    second = index.search("rotate_token", cursor=first["next_cursor"], limit=1)
    assert second["next_cursor"] is None
    assert second["hits"][0]["path"] == "c.py"


def test_symbol_cache_is_bounded_and_invalidates_on_change(tmp_path, monkeypatch):
    monkeypatch.setattr("proto.tools.code_index.FILE_BYTES", 64)
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "service.py"
    target.write_text("def old_token():\n    return 'old-value'\n", encoding="utf-8")
    index = CodeIndex(root)
    assert index.search("old token")["hits"][0]["symbol"] == "old_token"
    cached = next(iter(index.cache.values()))
    assert all(isinstance(row, tuple) for row in cached[1][0])
    assert "old-value" not in repr(index.cache)
    target.write_text("def renew_session():\n    return 'new-value'\n", encoding="utf-8")
    stamp_ns = target.stat().st_mtime_ns + 1
    target.touch()
    os.utime(target, ns=(stamp_ns, stamp_ns))
    assert index.search("renew session")["hits"][0]["symbol"] == "renew_session"
    assert index.search("old token")["hits"] == []


def test_index_skips_or_degrades_unsafe_and_unparsable_sources(tmp_path, monkeypatch):
    monkeypatch.setattr("proto.tools.code_index.FILE_BYTES", 40)
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dependency.py").write_text("def verify_access_token(): pass\n", encoding="utf-8")
    (root / ".hidden.py").write_text("def verify_access_token(): pass\n", encoding="utf-8")
    for name in ("binary", "large", "broken_api"):
        (root / name).mkdir()
    (root / "binary" / "source.py").write_bytes(b"def verify_access_token(): pass\n\x00")
    (root / "large" / "source.py").write_text("def verify_access_token(): pass\n" * 4, encoding="utf-8")
    (root / "broken_api" / "source.py").write_text("def (broken", encoding="utf-8")
    index = CodeIndex(root)
    assert list(index.files(root)) == [root / "binary" / "source.py",
                                       root / "broken_api" / "source.py",
                                       root / "large" / "source.py"]
    binary = index.search("verify access token", cwd="binary")
    assert binary["hits"] == [] and binary["coverage"]["skipped"] == 1
    large = index.search("verify access token", cwd="large")
    assert large["hits"] == [] and large["coverage"]["file_only"] == 1
    degraded = index.search("broken api", cwd="broken_api")
    assert degraded["coverage"]["file_only"] == 1
    assert degraded["hits"][0]["path"] == "broken_api/source.py"
    with pytest.raises(ValueError, match="invalid_exploration_path"):
        index.search("verify access token", cwd=".hidden.py")


def test_project_search_cannot_switch_to_sibling_directory(tmp_path):
    agent = make_agent(tmp_path)
    make_project(agent.code_workspace.root)
    agent.llm.decisions = [
        {"tool": "search_symbols", "args": {"query": "verify_access_token", "cwd": "projects/beta"}},
        {"done": True, "summary": "未读取兄弟项目"},
    ]
    result = AgenticLoop(agent, max_steps=4).run("分析本项目认证", project_dir="projects/app")
    assert result["status"] == "completed"
    assert any("exploration_path_outside_scope" in observation for observation in result["observations"])
    session = agent.sessions.lookup(result["session_id"])
    assert session["trace"] == []


def test_agent_reads_only_symbol_window_after_search(tmp_path):
    agent = make_agent(tmp_path)
    make_project(agent.code_workspace.root)
    agent.llm.decisions = [
        {"tool": "search_symbols", "args": {"query": "verify_access_token"}},
        {"tool": "read_file", "args": {"path": "projects/app/z_auth.py", "offset": 3, "limit": 4}},
        {"done": True, "summary": "已定位认证校验函数"},
    ]
    result = AgenticLoop(agent, max_steps=5).run("查看认证逻辑", project_dir="projects/app")
    assert result["status"] == "completed"
    assert [row["tool"] for row in result["trace"]] == ["search_symbols", "read_file"]
    assert "verify_access_token" in result["observations"][-2]


def test_explorer_reads_in_child_context_and_parent_gets_report_only(tmp_path):
    agent = make_agent(
        tmp_path,
        ScriptModel(
            decisions=[
                {"tool": "explore_code", "args": {"question": "verify access token"}},
                {"done": True, "summary": "调查完成"},
            ],
            explorer=[
                {"tool": "read_file", "args": {"path": "projects/app/z_auth.py", "offset": 3, "limit": 5}},
                {"done": True, "summary": "Auth.verify_access_token 校验传入 token。"},
            ]))
    make_project(agent.code_workspace.root)
    result = AgenticLoop(agent, max_steps=5).run("说明认证模块", project_dir="projects/app")
    assert result["status"] == "completed"
    assert len(agent.llm.explorer_inputs) == 2
    assert "AUTH-CHILD-CONTEXT-SECRET" in agent.llm.explorer_inputs[1][1]
    report = next(observation for observation in result["observations"] if "explore_code(" in observation)
    assert "AUTH-CHILD-CONTEXT-SECRET" not in report
    assert "projects/app/z_auth.py" in report
    assert "AUTH-CHILD-CONTEXT-SECRET" not in agent.llm.actor_inputs[-1][1]
    session = agent.sessions.lookup(result["session_id"])
    assert "AUTH-CHILD-CONTEXT-SECRET" not in json.dumps(session["events"], ensure_ascii=False)


def test_explore_code_requires_active_execution_context():
    assert explore_code("认证在哪里") == {"status": "incomplete", "reason": "active_execution_required",
                                          "references": []}


def test_investigator_rejects_write_tool_escape_and_source_echo(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    make_project(root)
    index = CodeIndex(root)
    context = {"workspace": str(root.resolve()), "project_dir": "projects/app",
               "instructions": {}, "active_instructions": []}
    for reply, reason in (
            ({"tool": "run_command", "args": {"argv": ["ls"]}}, "explorer_tool_not_allowed"),
            ({"tool": "read_file", "args": {"path": "../outside.py", "offset": 0, "limit": 2}},
             "invalid_exploration_request")):
        result = investigate(index, context, "verify access token", "projects/app",
                             lambda *_: json.dumps(reply), lambda: "")
        assert result["status"] == "incomplete" and result["reason"] == reason
    echo = "return token == 'AUTH-CHILD-CONTEXT-SECRET'"
    echo_replies = iter([
        json.dumps({"tool": "read_file", "args": {"path": "projects/app/z_auth.py", "offset": 3, "limit": 5}}),
        json.dumps({"done": True, "summary": echo}),
    ])
    result = investigate(index, context, "verify access token", "projects/app",
                         lambda *_: next(echo_replies), lambda: "")
    assert result["status"] == "incomplete" and result["reason"] == "source_echo"


def test_decision_input_compacts_history_without_provider_usage():
    old_source = "OLD-SOURCE-BODY-" * 900
    # M82：最近读过的两个文件保留正文；old.py 之后又读了两个文件，所以被折叠成指针。
    observations = [f"read_file({{'path': 'old.py', 'offset': 0}}) → {old_source}",
                    'read_file({"path": "recent1.py", "offset": 0}) → RECENT-ONE',
                    'read_files({"paths": ["recent2.py"]}) → RECENT-TWO',
                    "grep({'pattern': 'needle'}) → needle"]
    trace = [{"tool": "explore_code", "args": {"question": "auth"},
              "result": {"status": "ok", "summary": "认证在 auth.py。",
                         "references": [{"path": "auth.py", "line": 3, "end_line": 9}]}},
             {"tool": "read_file", "args": {"path": "old.py", "offset": 0}, "result": old_source},
             {"tool": "grep", "args": {"pattern": "needle"}, "result": "needle"}]
    output = decision_input("系统指令", "固定规则：不许静默裁剪。", "辅助信息" * 100,
                            observations, trace, 32768)
    assert "OLD-SOURCE-BODY" not in output and "RECENT-ONE" in output and "RECENT-TWO" in output
    assert "old.py" in output and "固定规则" in output
    assert "认证在 auth.py" in output and "auth.py:3-9" in output
    assert size("系统指令" + output) <= 32768
    assert observations[0].startswith("read_file(")


def test_most_recent_file_read_keeps_a_bounded_body():
    # M82 的核心修复：修错误时模型要能看见刚读过的文件，否则只能凭想象整文件重写。
    source = "RECENT-SOURCE-BODY-" * 900
    observations = [f"read_file({{'path': 'recent.py', 'offset': 0}}) → {source}",
                    "grep({'pattern': 'needle'}) → needle"]
    output = decision_input("系统指令", "固定规则：不许静默裁剪。", "", observations, [], 65536)
    assert "RECENT-SOURCE-BODY" in output and "正文在此截断" in output
    assert output.count("RECENT-SOURCE-BODY") < 900
    assert size("系统指令" + output) <= 65536


def test_tight_context_drops_advisory_and_large_recent_body():
    body = "RECENT-LONG-BODY-" * 200
    observations = [f"read_file({{'path': 'recent.py', 'offset': 20}}) → {body}"]
    trace = [{"tool": "read_file", "args": {"path": "recent.py", "offset": 20}, "result": body}]
    output = decision_input("系统", "固定", "MUST-DROP-ADVISORY", observations, trace,
                            32768, tight=True)
    assert "MUST-DROP-ADVISORY" not in output
    assert "RECENT-LONG-BODY" not in output
    assert "recent.py" in output and size("系统" + output) <= 32768


def test_oversized_fixed_context_fails_closed():
    with pytest.raises(ValueError, match="context_budget_exceeded"):
        decision_input("系统", "固定" * 20000, "", [], [], 32768)


def test_provider_context_limit_retries_once_with_tighter_projection(tmp_path):
    agent = make_agent(tmp_path, ContextLimitModel())
    make_project(agent.code_workspace.root)
    result = AgenticLoop(agent, max_steps=3).run("回答只读问题")
    assert result["status"] == "completed"
    assert len(agent.llm.actor_inputs) == 2
    assert size("".join(agent.llm.actor_inputs[1])) < size("".join(agent.llm.actor_inputs[0]))
    session = agent.sessions.lookup(result["session_id"])
    assert session["tight_context"] is True
    assert session["trace"] == []


def test_truncated_decision_retries_small_step_without_replaying_tools(tmp_path):
    agent = make_agent(tmp_path, OutputLimitModel())
    make_project(agent.code_workspace.root)
    result = AgenticLoop(agent, max_steps=3).run("继续设计规格", project_dir="projects/app")
    assert result["status"] == "completed"
    assert len(agent.llm.actor_inputs) == 2
    assert "未执行任何工具" in agent.llm.actor_inputs[1][1]
    assert "禁止 write_file 重发长文件" in agent.llm.actor_inputs[1][1]
    session = agent.sessions.lookup(result["session_id"])
    assert session["compact_decisions"] is True
    assert session["trace"] == [] and session["pending_call"] is None


def test_waiting_truncated_session_resumes_in_small_decision_mode(tmp_path):
    agent = make_agent(tmp_path, ScriptModel([{"done": True, "summary": "完成"}]))
    make_project(agent.code_workspace.root)
    first = AgenticLoop(agent, max_steps=2).run("读规格", project_dir="projects/app")
    session = agent.sessions.lookup(first["session_id"])
    session.update(status="waiting_model", pending_decision=None, pending_call=None,
                   result={"status": "waiting_model", "reason": "llm_error:model_output_truncated"})
    agent.sessions.save(session)
    agent.llm.decisions = [{"done": True, "summary": "完成"}]
    result = AgenticLoop(agent).resume(session["id"], True)
    assert result["status"] == "completed"
    resumed = agent.sessions.lookup(session["id"])
    assert resumed["compact_decisions"] is True
    assert "上一决策因模型输出上限被丢弃" in agent.llm.actor_inputs[-1][1]


def test_write_and_edit_decision_arguments_have_output_bounds():
    with pytest.raises(ValueError):
        checked_arguments("write_file", {"path": "a.md", "content": "x" * 6001},
                          builtin_parameters("write_file"))
    with pytest.raises(ValueError):
        checked_arguments("edit_file", {"path": "a.md", "old": "x" * 4001, "new": "y"},
                          builtin_parameters("edit_file"))
