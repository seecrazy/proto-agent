# -*- coding: utf-8 -*-
"""文件与执行工具包：CodeTools 12 工具（原 CodeTools.register_all 声明，M78 迁移）。

实现仍全部在 proto/tools/codetools.py 的 CodeTools 类；本文件只声明注册面。
run_python/run_command 的能力级别随后端动态决定：沙盒后端（写遏制+禁网）
等同 WORKSPACE_WRITE，未隔离后端为 VISIBLE_WRITE 且主人超时也绝不自主代批。
"""
from proto.kernel.kernel import ActionCategory
from proto.plugins.base import ToolSpec
from proto.tools.codetools import explore_code, read_skill
from proto.skills import catalog as skill_catalog
from proto.exec.engineering import inspect_build


def specs(code_tools) -> list:
    backend = code_tools.execution_backend()
    run_category = (ActionCategory.WORKSPACE_WRITE
                    if backend in ("runsc", "sandbox-exec")
                    else ActionCategory.VISIBLE_WRITE)
    run_owner_only = backend not in ("runsc", "sandbox-exec")

    return [
        ToolSpec(
            name="read_file",
            fn=lambda path, offset=0, limit=200: code_tools.read_file(path, offset, limit),
            category=ActionCategory.READ_DRAFT,
            description="读工作区文件（行级分页：offset/limit）",
            read_only=True, obs_limit=3000, background_io=True),
        ToolSpec(
            name="read_files",
            fn=lambda paths=None: code_tools.read_files(paths),
            category=ActionCategory.READ_DRAFT,
            description="批量读多个文件（≤5 个，各前 60 行）",
            read_only=True, obs_limit=6000, background_io=True),
        ToolSpec(
            name="write_file",
            fn=lambda path, content: code_tools.write_file(path, content),
            category=ActionCategory.WORKSPACE_WRITE,
            description="写工作区文件；content ≤6000 字符，长文件先骨架再分次 edit",
            background_io=True),
        ToolSpec(
            name="edit_file",
            fn=lambda path, old, new, replace_all=False: code_tools.edit_file(path, old, new, replace_all),
            category=ActionCategory.WORKSPACE_WRITE,
            description="精确替换工作区文件片段；old/new 各 ≤4000 字符，优先小补丁",
            background_io=True),
        ToolSpec(
            name="list_dir",
            fn=lambda path="": code_tools.list_dir(path),
            category=ActionCategory.READ_DRAFT,
            description="列工作区目录",
            read_only=True, background_io=True),
        ToolSpec(
            name="grep",
            fn=code_tools.grep,
            category=ActionCategory.READ_DRAFT,
            description="在指定目录流式搜索文本，返回命中行和位置",
            read_only=True, obs_limit=1000, background_io=True),
        ToolSpec(
            name="search_symbols",
            fn=code_tools.code_index.search,
            category=ActionCategory.READ_DRAFT,
            description="按标识符/文档词找代码声明；返回路径和行号。next_cursor 非空仅表示本页，原查询可续页",
            read_only=True, obs_limit=3200, background_io=True),
        ToolSpec(
            name="explore_code",
            fn=explore_code,
            category=ActionCategory.READ_DRAFT,
            description="执行会话内委派只读代码调查，子上下文自行检索、分段读取，仅返回短结论和已读位置",
            read_only=True, obs_limit=2400, background_io=True),
        ToolSpec(
            name="read_skill",
            fn=read_skill,
            category=ActionCategory.READ_DRAFT,
            description="按需加载一份内置工作法（不授予权限）：" + skill_catalog(),
            read_only=True, obs_limit=2500),
        ToolSpec(
            name="inspect_build",
            fn=lambda cwd="": inspect_build(code_tools, cwd),
            category=ActionCategory.READ_DRAFT,
            description="只读核对工位、沙盒、构建入口和本机工具；next 可传给 run_command，不表示已编译",
            read_only=True, obs_limit=3600, background_io=True),
        ToolSpec(
            name="run_python",
            fn=lambda code, timeout_s=60: code_tools.run_python(code, timeout_s),
            category=run_category,
            description="沙盒跑 Python（验证/测试）",
            owner_only=run_owner_only, obs_limit=1500, background_io=True),
        ToolSpec(
            name="run_command",
            fn=lambda argv, cwd="", timeout_s=120: code_tools.run_command(argv, cwd, timeout_s),
            category=run_category,
            description="沙盒跑白名单本机验证命令（数组参数，无 shell）",
            owner_only=run_owner_only, obs_limit=4000, background_io=True),
    ]
