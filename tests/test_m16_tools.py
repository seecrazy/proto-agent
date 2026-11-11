# -*- coding: utf-8 -*-
"""M16a: 编码工具族（工作区文件/检索/Python 沙盒执行）+ workspace_write 分级。"""
import pytest

from proto.exec.codetools import CodeTools
from proto.kernel.kernel import ActionCategory, Kernel
from proto.kernel.anchor import OwnerAnchor
from proto.kernel.audit import AuditSpine
from proto.kernel.budgets import BudgetPools
from proto.state.store import FileSystemStore


@pytest.fixture()
def tools(tmp_path):
    return CodeTools(workspace_root=tmp_path / "ws")


@pytest.fixture()
def kernel(tmp_path):
    store = FileSystemStore(tmp_path / "d")
    spine = AuditSpine(store)
    anchor = OwnerAnchor.enroll(store, spine, b"\x01" * 32)
    return Kernel(anchor, spine,
                  BudgetPools({"global": {"tokens": 1e6}, "owner_tasks": {"tokens": 1e6},
                               "self_initiated": {"tokens": 1e5}}))


class TestFileTools:
    def test_write_read_list_roundtrip(self, tools):
        tools.write_file("src/main.py", "print('hi')\n")
        assert tools.read_file("src/main.py") == "print('hi')\n"
        listing = tools.list_dir("")
        assert "src/" in listing

    def test_edit_file_replaces_unique(self, tools):
        tools.write_file("a.txt", "hello world")
        n = tools.edit_file("a.txt", "world", "proto")
        assert n == 1 and tools.read_file("a.txt") == "hello proto"

    def test_path_traversal_blocked(self, tools):
        with pytest.raises(ValueError):
            tools.read_file("../escape.txt")
        with pytest.raises(ValueError):
            tools.write_file("/abs/path.txt", "x")

    def test_grep_finds_matches(self, tools):
        tools.write_file("x/a.py", "def foo():\n    return 1\n")
        tools.write_file("x/b.py", "def bar():\n    return 2\n")
        hits = tools.grep("def foo")
        assert len(hits) == 1 and "a.py" in hits[0]["path"]

    def test_read_missing_returns_none(self, tools):
        assert tools.read_file("nope.txt") is None

    def test_read_size_capped(self, tools):
        tools.write_file("big.txt", "x" * 60000)
        assert len(tools.read_file("big.txt")) <= 50000


class TestPythonRunner:
    def test_run_python_basic(self, tools):
        r = tools.run_python("print(1+1)")
        assert r["exit"] == 0 and r["stdout"].strip() == "2"

    def test_run_python_stderr_and_nonzero(self, tools):
        r = tools.run_python("import sys; sys.stderr.write('boom'); sys.exit(3)")
        assert r["exit"] == 3 and "boom" in r["stderr"]

    def test_run_python_timeout(self, tools):
        r = tools.run_python("import time; time.sleep(30)", timeout_s=2)
        assert r["exit"] != 0 and r["timeout"] is True

    def test_run_python_cannot_see_host_env(self, tools, monkeypatch):
        monkeypatch.setenv("PROTO_RUNNER_PROBE", "secret")
        r = tools.run_python("import os; print(os.environ.get('PROTO_RUNNER_PROBE','CLEAN'))")
        assert "CLEAN" in r["stdout"]


class TestWorkspaceWriteCategory:
    def test_workspace_write_auto_allowed(self, kernel):
        d = kernel.vet_action(ActionCategory.WORKSPACE_WRITE, "工作区内写文件")
        assert d.allowed and not d.requires_human

    def test_irreversible_still_human_only(self, kernel):
        d = kernel.vet_action(ActionCategory.IRREVERSIBLE, "rm -rf")
        assert d.requires_human


class TestRouterWorkspace:
    def test_workspace_write_step_lands_in_workspace(self, tmp_path, kernel):
        from proto.exec.approval import ApprovalQueue
        from proto.exec.checkpoints import CheckpointStore
        from proto.exec.router import Router, Task, ToolRegistry
        from proto.exec.workspace import ScratchWorkspace
        from proto.state.store import FileSystemStore as FS

        store = FS(tmp_path / "d")
        spine = AuditSpine(store)
        tools = ToolRegistry()
        ct = CodeTools(workspace_root=tmp_path / "ws")
        ct.register_all(tools)
        router = Router(kernel, ApprovalQueue(kernel, spine),
                        ScratchWorkspace(tmp_path / "s", spine),
                        CheckpointStore(store), tools, code_workspace=ct)
        result = router.run(Task(id="t1", description="写代码", steps=[
            {"tool": "write_file", "args": {"path": "hello.py", "content": "print('ok')"},
             "category": "workspace_write"}]))
        assert result.status == "completed"
        assert (tmp_path / "ws" / "hello.py").read_text() == "print('ok')"
