# -*- coding: utf-8 -*-
"""结构不变量：目录分层即架构承诺（M78）。

解析 proto/ 全部源码的 proto.* import，钉住依赖方向：
- L1 内核纯净：kernel 只依赖自身与 storage；
- L2 状态纯净：state 只依赖 kernel/storage/自身；
- 执行层不上行：exec 核心模块不得依赖 tools/runtime/web/agents/appshell/adapters
  （工具装配只发生在组合根 runtime/agent.py）；
- 工具包不碰循环核心：tools 只能使用 exec 的共享库层。

已知残留例外必须显式登记在 KNOWN_UPWARD，消除时同步删除条目（棘轮式收紧）。
"""
import os
import re

import pytest

PROTO_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "proto")

_IMPORT = re.compile(r"^\s*(?:from|import)\s+(proto\.[\w.]+)", re.M)

EXEC_SHARED = {"contracts", "engineering", "exploration", "work_context"}
EXEC_CORE = {
    "router", "agentic", "sessions", "context", "contracts", "injector",
    "checkpoints", "approval", "workspace", "planner", "plan_evidence",
    "proposal_writing", "work_context", "modulators", "exploration", "engineering",
}
UPPER_PACKAGES = ("proto.tools", "proto.runtime", "proto.web", "proto.agents",
                  "proto.appshell", "proto.adapters")

# 已知上行依赖（阶段 3 迁移 runcontext 后应清空 runtime 条目）
KNOWN_UPWARD = {
    ("exec.router", "proto.runtime.runcontext"),
    ("exec.agentic", "proto.runtime.runcontext"),
}


def _module_imports():
    """返回 {模块名: set(proto.xxx 依赖)}，覆盖 proto/ 下全部 .py。"""
    graph = {}
    for dirpath, dirnames, filenames in os.walk(PROTO_ROOT):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), PROTO_ROOT)
            module = rel[:-3].replace(os.sep, ".")
            if module.endswith(".__init__"):
                module = module[: -len(".__init__")]
            with open(os.path.join(dirpath, name), encoding="utf-8") as fh:
                source = fh.read()
            graph[module] = set(_IMPORT.findall(source))
    return graph


@pytest.fixture(scope="module")
def graph():
    return _module_imports()


def _external(module, deps):
    """模块的 proto.* 依赖中，去掉同包部分。"""
    prefix = "proto." + module.split(".")[0] + "."
    return {d for d in deps if not d.startswith(prefix)}


class TestLayerPurity:
    def test_kernel_only_depends_on_storage(self, graph):
        for module, deps in graph.items():
            if module.startswith("kernel."):
                external = _external(module, deps)
                allowed = {d for d in external
                           if d.startswith("proto.storage.") or d.startswith("proto.kernel.")}
                assert external - allowed == set(), f"{module} 越界依赖: {external - allowed}"

    def test_state_only_depends_on_kernel_storage(self, graph):
        for module, deps in graph.items():
            if module.startswith("state."):
                external = _external(module, deps)
                allowed = {d for d in external if d.startswith(("proto.storage.", "proto.kernel."))}
                assert external - allowed == set(), f"{module} 越界依赖: {external - allowed}"


class TestExecIsolation:
    def test_exec_core_does_not_depend_upward(self, graph):
        violations = {}
        for module, deps in graph.items():
            if not module.startswith("exec.") or module.split(".")[1] not in EXEC_CORE:
                continue
            bad = {d for d in deps
                   if d.startswith(UPPER_PACKAGES)
                   and (module, d) not in KNOWN_UPWARD}
            if bad:
                violations[module] = bad
        assert violations == {}, f"exec 核心出现上行依赖: {violations}"

    def test_known_upward_matches_reality(self, graph):
        """已知例外必须与真实依赖一一对应：新增违例会失败，偷偷消除也要求更新登记。"""
        real = set()
        for module, deps in graph.items():
            if not module.startswith("exec.") or module.split(".")[1] not in EXEC_CORE:
                continue
            for dep in deps:
                if dep.startswith(UPPER_PACKAGES):
                    real.add((module, dep))
        assert real == set(KNOWN_UPWARD), f"已知例外与实际不符: 新增={real - set(KNOWN_UPWARD)}, 已消={set(KNOWN_UPWARD) - real}"


class TestToolsBoundaries:
    def test_tools_do_not_import_exec_core(self, graph):
        violations = {}
        for module, deps in graph.items():
            if not module.startswith("tools."):
                continue
            bad = set()
            for dep in deps:
                if dep.startswith("proto.exec."):
                    submodule = dep.split(".")[2]
                    if submodule not in EXEC_SHARED:
                        bad.add(dep)
            if bad:
                violations[module] = bad
        assert violations == {}, f"tools 依赖了 exec 循环核心: {violations}"

    def test_tools_present(self, graph):
        for name in ("codetools", "webtools", "fleettools", "bridge", "code_index"):
            assert f"tools.{name}" in graph, f"proto/tools/{name}.py 应存在"
