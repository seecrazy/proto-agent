# -*- coding: utf-8 -*-
"""舰队协调存储：队列/票据/项目/对话回执/模型冷却/公司数据（state.sqlite3）。

零 proto 依赖的独立存储包（M78 从 runtime 拆出），被 runtime/fleet/exec(tools)/web
共同使用；实现见 coordination.py。
"""
from proto.coordination.coordination import (  # noqa: F401
    CoordinationDB,
    CoordinationError,
    encode,
)
