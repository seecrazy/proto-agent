# -*- coding: utf-8 -*-
"""M5: LLM 步骤规划器——把"主人想做什么"翻译成 Router 可执行的工具步骤。

安全边界：规划输出必须过工具白名单与动作分级白名单，
未知工具/非法分级一律丢弃；解析失败返回空（运行时退化为纯对话）。
"""
from __future__ import annotations

from typing import List

from proto.exec.router import ToolRegistry
from proto.metacog.attribution import extract_json_array

VALID_CATEGORIES = {"read_draft", "visible_write", "irreversible"}


class LLMPlanner:
    def __init__(self, client, tools: ToolRegistry):
        self._client = client  # 建议传快模型（低风险高频）
        self._tools = tools

    def _prompt(self) -> str:
        names = ", ".join(sorted(self._tools.names()))
        return (
            "你是成长型 Agent PROTO 的任务规划器。把任务拆解为工具调用步骤。"
            f"可用工具：{names}，以及内置 write_file(path, content)（写文件草稿）。\n"
            "每步格式：{\"tool\": ..., \"args\": {...}, \"category\": "
            "\"read_draft|visible_write|irreversible\"}。\n"
            "只读类动作用 read_draft；会改动可见文件用 visible_write；不可逆动作用 "
            "irreversible（需人工批准）。只输出 JSON 数组，无法规划就输出 []。"
        )

    def plan(self, description: str) -> List[dict]:
        try:
            rows = extract_json_array(self._client.complete(self._prompt(), description))
        except Exception:
            return []
        steps = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            tool = row.get("tool")
            if tool != "write_file" and tool not in self._tools.names():
                continue
            category = row.get("category", "read_draft")
            if category not in VALID_CATEGORIES:
                category = "read_draft"
            steps.append({"tool": tool, "args": row.get("args", {}) or {},
                          "category": category})
        return steps
