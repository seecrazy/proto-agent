# -*- coding: utf-8 -*-
"""M16: 观察-行动 Agent 循环——PROTO 从"执行既定步骤"到"自主决定下一步"。

每步：把任务 + 工具清单 + 全部历史观察喂给模型 → 模型决定下一个工具调用
（或宣布完成）→ 过 Router 安全管线执行（工作区内自动、对外审批暂停可恢复）
→ 观察回灌 → 下一轮。max_steps 防失控，每步检查点可断点续传。
"""
from __future__ import annotations

import json
import re
import time
from typing import Dict, List

LOOP_SYSTEM = (
    "你是 PROTO 的任务执行循环。根据任务与已有观察，决定下一个工具调用，"
    "或宣布完成。\n"
    '每次只输出一个 JSON：{"tool": ..., "args": {...}, '
    '"category": "read_draft|workspace_write|visible_write"} '
    '或 {"done": true, "summary": "成果摘要"}。\n'
    "规则：工作区内的探索/读写/跑代码用 workspace_write 或 read_draft；"
    "会影响工作区外可见世界时才用 visible_write（会暂停等主人批准）。"
    "观察足够得出结论时立即 done，不要为了用工具而用工具。"
)


def _parse_decision(raw: str) -> dict:
    cleaned = re.sub(r"```(?:json)?", "", raw).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        return {"done": True, "summary": cleaned[:200] or "（无输出）"}
    try:
        return json.loads(cleaned[start:end + 1])
    except ValueError:
        return {"done": True, "summary": cleaned[:200]}


class AgenticLoop:
    def __init__(self, agent, max_steps: int = 12):
        self._agent = agent
        self.max_steps = max_steps

    def _tool_catalog(self) -> str:
        names = sorted(self._agent.tools.names())
        hints = {
            "read_file": "读工作区文件", "write_file": "写工作区文件",
            "edit_file": "精确替换文件片段", "list_dir": "列工作区目录",
            "grep": "正则搜索工作区", "run_python": "沙盒跑 Python（验证/测试）",
            "clock": "当前时间", "delegate_coding": "外包给外部编码 agent（重活）",
        }
        return "\n".join(f"- {n}: {hints.get(n, '')}" for n in names)

    def run(self, task_text: str) -> Dict:
        if self._agent.llm is None:
            return {"status": "no_llm",
                    "reason": "Agentic 循环需要 LLM（离线模式请用直接步骤）"}
        observations: List[str] = []
        task_id = f"loop-{int(time.time() * 1000)}"
        for step in range(self.max_steps):
            user = (f"任务：{task_text}\n\n可用工具：\n{self._tool_catalog()}\n\n"
                    f"已有观察：\n"
                    + ("\n".join(f"{i+1}. {o}" for i, o in enumerate(observations[-12:]))
                       or "（无）"))
            try:
                raw = self._agent.llm.complete(LOOP_SYSTEM, user)
            except Exception as exc:
                return {"status": "failed", "reason": f"llm_error:{exc}",
                        "observations": observations}
            decision = _parse_decision(raw)
            if decision.get("done"):
                return {"status": "completed", "summary": decision.get("summary", ""),
                        "observations": observations}
            tool = decision.get("tool", "")
            if tool not in self._agent.tools.names():
                observations.append(f"错误：未知工具 {tool!r}（从清单里选）")
                continue
            from proto.exec.router import Task
            step_task_id = f"{task_id}-{step}"
            task = Task(id=step_task_id, description=task_text,
                        steps=[{"tool": tool, "args": decision.get("args", {}),
                                "category": decision.get("category", "read_draft")}])
            result = self._agent.router.run(task)
            if result.status == "paused":
                self._agent.scheduler.on_task_completed(result)
                return {"status": "paused", "task_id": step_task_id,
                        "proposal_id": result.proposal_id,
                        "observations": observations,
                        "reason": "等待主人批准（approve 后 resume_task 续跑）"}
            if result.status in ("failed", "denied", "no_budget"):
                observations.append(f"{tool} → {result.status}: {result.reason}")
                continue
            observation = result.step_results[0] if result.step_results else ""
            observations.append(f"{tool} → {str(observation)[:600]}")
        self._agent.scheduler.on_task_completed(
            type("R", (), {"status": "completed", "retries": 0, "reason": ""})())
        return {"status": "max_steps", "observations": observations}
