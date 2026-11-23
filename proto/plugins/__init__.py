# -*- coding: utf-8 -*-
"""proto.plugins：工具插件装配（M78）。

BUILTIN_PACKS 是显式清单——项目风格：可审计、无自动发现。新增工具族 =
新写一个 pack_*.py 的 specs() 并加入 assemble_tools 的组合处；
单个工具的声明（实现/能力级别/参数/只读/观察限额/后台 IO）全部收在
ToolSpec 一处，见 base.py。
"""
from proto.plugins import pack_bridge, pack_core, pack_files, pack_fleet, pack_subagent, pack_web


def assemble_tools(registry, agent) -> None:
    """把全部内置 pack 装配进注册表。agent 为组合根 ProtoAgent。"""
    spec_groups = (
        pack_core.specs(agent),
        pack_files.specs(agent.code_workspace),
        pack_web.specs(agent.webtools),
        pack_fleet.specs(agent),
        pack_bridge.specs(agent),
        pack_subagent.specs(agent),
    )
    for specs in spec_groups:
        for spec in specs:
            spec.register_into(registry)
