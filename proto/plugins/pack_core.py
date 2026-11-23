# -*- coding: utf-8 -*-
"""核心杂项工具包：时钟 + 中文文风扫描/润色（原 agent.py 内联注册，M78 迁移）。"""
from proto.kernel.kernel import ActionCategory
from proto.plugins.base import ToolSpec


def specs(agent):
    from proto.runtime.self_facts import local_time
    from proto.textstyle import polish_style, scan_style

    return [
        ToolSpec(
            name="clock",
            fn=lambda: local_time(agent._now()) or "当前服务端时间不可用",
            category=ActionCategory.READ_DRAFT,
            description="当前时间",
            read_only=True),
        ToolSpec(
            name="scan_style",
            fn=scan_style,
            category=ActionCategory.READ_DRAFT,
            description="离线扫描中文模板腔，返回位置和处方，不改稿",
            read_only=True, obs_limit=4000),
        ToolSpec(
            name="polish_style",
            fn=polish_style,
            category=ActionCategory.READ_DRAFT,
            description="对传入的中文短稿按处方局部改写并复验，失败退回原文；不写文件",
            read_only=True, obs_limit=5200),
    ]
