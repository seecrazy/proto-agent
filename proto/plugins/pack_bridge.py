# -*- coding: utf-8 -*-
"""外部编码 CLI 桥工具包：探测到 claude/codex/zcode 才注册 delegate_coding（M78 迁移）。

VISIBLE_WRITE + owner_only：每次调用都需主人批准，主人超时也绝不自主代批。
"""
from proto.kernel.kernel import ActionCategory
from proto.plugins.base import ToolSpec


def specs(agent) -> list:
    bridge = getattr(agent, "bridge", None)
    if not (bridge and bridge.available):
        return []
    return [
        ToolSpec(
            name="delegate_coding",
            fn=lambda task: agent._delegate_output(task),
            category=ActionCategory.VISIBLE_WRITE,
            description="外包给外部编码 agent（需审批）",
            owner_only=True, background_io=True),
    ]
