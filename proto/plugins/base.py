# -*- coding: utf-8 -*-
"""ToolPack 插件规范：一个工具的全部声明收在一个地方。

新增一个工具 = 写一个返回 [ToolSpec] 的 pack 函数并加入 assemble_tools 清单，
不再需要分别改 contracts._BUILTINS、agent 装配段和 agentic 的三个硬编码清单。

安全边界不变：category 只能由 ToolSpec 声明，Router 对模型自行申报的类别
只取更高者（见 exec/router.py _effective_category）；parameters 缺省回落
contracts.builtin_parameters，再缺省由签名推导，schema 校验照旧。
"""
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from proto.kernel.kernel import ActionCategory


@dataclass
class ToolSpec:
    """单个工具的完整声明：实现、能力级别、参数与行为元数据。"""
    name: str
    fn: Callable
    category: ActionCategory
    description: str = ""
    owner_only: bool = False
    parameters: Optional[Dict] = None
    read_only: bool = False        # 只读探索步计数（连续探索强制转向产出）
    obs_limit: Optional[int] = None  # 观察截断字符数；None=默认 600
    background_io: bool = False    # 耗时 I/O，执行时切后台

    def register_into(self, registry) -> None:
        from proto.exec.contracts import builtin_parameters
        category = self.category
        if isinstance(category, str):
            category = ActionCategory[category]  # 容许 pack 作者写 "READ_DRAFT" 名字串
        parameters = self.parameters
        if parameters is None:
            try:
                parameters = builtin_parameters(self.name)
            except KeyError:
                parameters = None  # 无内置 schema 时由注册表从签名推导
        registry.register(self.name, self.fn, category=category,
                          description=self.description, owner_only=self.owner_only,
                          parameters=parameters, read_only=self.read_only,
                          obs_limit=self.obs_limit, background_io=self.background_io)
