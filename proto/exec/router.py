# -*- coding: utf-8 -*-
"""M4-②: Router/Worker 执行器骨架（设计 §8）。

Router 串行调度任务：预算预留 → 逐步执行 → 每步检查点 → 对外动作走审批
（暂停于检查点，批准恢复 / 拒绝中止且产物留在 scratch）。
步骤的产生者后续由 LLM 策略接管（决定调用什么工具），安全管线不变。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from proto.exec.approval import ActionProposal, ApprovalQueue
from proto.exec.checkpoints import CheckpointStore
from proto.exec.workspace import ScratchWorkspace
from proto.kernel.kernel import ActionCategory, Kernel

DEFAULT_STEP_COST = {"tokens": 20.0}
VARIANT_ID = "core@0.3.0"


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, Callable] = {}

    def register(self, name: str, fn: Callable) -> None:
        self._tools[name] = fn

    def get(self, name: str) -> Callable:
        if name not in self._tools:
            raise KeyError(f"unknown_tool:{name}")
        return self._tools[name]

    def names(self):
        return set(self._tools)


@dataclass
class Task:
    id: str
    description: str
    steps: List[dict]  # {"tool", "args", "category"}
    owner_assigned: bool = True
    desire_dim: Optional[str] = None  # 自主任务关联的欲望维度（阻尼/预算语义）


@dataclass
class TaskResult:
    task_id: str
    status: str  # completed | paused | denied | failed | no_budget
    step_results: List = field(default_factory=list)
    reason: str = ""
    proposal_id: Optional[str] = None
    retries: int = 0
    next_step: int = 0


class Router:
    def __init__(self, kernel: Kernel, queue: ApprovalQueue, workspace: ScratchWorkspace,
                 checkpoints: CheckpointStore, tools: ToolRegistry,
                 step_cost: Dict[str, float] = None, code_workspace=None):
        self._kernel = kernel
        self._queue = queue
        self._ws = workspace
        self._workspace = code_workspace   # CodeTools：工作区写自动放行
        self._cps = checkpoints
        self._tools = tools
        self._cost = step_cost or DEFAULT_STEP_COST

    def _pool(self, task: Task) -> str:
        return "owner_tasks" if task.owner_assigned else "self_initiated"

    def run(self, task: Task) -> TaskResult:
        total = {unit: amount * len(task.steps) for unit, amount in self._cost.items()}
        if not self._kernel.pools.try_consume(self._pool(task), total).granted:
            return TaskResult(task.id, "no_budget", reason="budget_pool_exhausted")
        return self._execute(task, start_step=0, step_results=[])

    def resume(self, task: Task, human_approved: bool) -> TaskResult:
        """从暂停检查点恢复：批准→继续余下步骤；拒绝→中止（产物留 scratch）。"""
        record = self._cps.resume(task.id)
        if record is None:
            return TaskResult(task.id, "failed", reason="no_checkpoint")
        state = record["state"]
        proposal_id = state.get("waiting_for")
        if proposal_id is None:
            return TaskResult(task.id, "failed", reason="not_paused")
        if not human_approved:
            self._queue.deny(proposal_id, reason="human_denied")
            return TaskResult(task.id, "denied", step_results=state.get("results", []),
                              reason="human_denied")
        approval = self._queue.approve(proposal_id)
        step = state["step"]
        # 待晋升的 scratch 产物持真实审批记录晋升
        pending_path = state.get("pending_promotion")
        if pending_path:
            self._ws.promote(pending_path, approval=approval)
        return self._execute(task, start_step=step + 1,
                             step_results=state.get("results", []))

    def _execute(self, task: Task, start_step: int, step_results: list) -> TaskResult:
        task_spec = {"description": task.description, "steps": task.steps,
                     "owner_assigned": task.owner_assigned,
                     "desire_dim": task.desire_dim}
        for i in range(start_step, len(task.steps)):
            step = task.steps[i]
            category = ActionCategory[step.get("category", "read_draft").upper()]
            self._cps.put(task.id, {
                "step": i, "results": step_results, "task": task_spec,
            }, variant_id=VARIANT_ID)
            if step["tool"] == "write_file":
                path = step["args"]["path"]
                if category is ActionCategory.WORKSPACE_WRITE and self._workspace is not None:
                    self._workspace.write_file(path, step["args"]["content"])
                    step_results.append(f"written:{path}")
                    continue
                self._ws.write(path, step["args"]["content"])
                if category is ActionCategory.VISIBLE_WRITE:
                    sub = self._queue.submit(ActionProposal(
                        category, f"晋升 {path}（{task.description}）",
                        {"path": path}, task_id=task.id,
                        self_initiated=not task.owner_assigned,
                        desire_dim=task.desire_dim))
                    if sub.rejected:
                        return TaskResult(task.id, "denied", step_results,
                                          reason=sub.reason, next_step=i)
                    if not sub.auto_approved:
                        # 暂停于检查点，等待人类
                        self._cps.put(task.id, {
                            "step": i, "results": step_results, "task": task_spec,
                            "waiting_for": sub.proposal_id,
                            "pending_promotion": path,
                        }, variant_id=VARIANT_ID)
                        return TaskResult(task.id, "paused", step_results,
                                          proposal_id=sub.proposal_id, next_step=i)
                step_results.append(f"written:{path}")
                continue
            try:
                fn = self._tools.get(step["tool"])
            except KeyError as exc:
                return TaskResult(task.id, "failed", step_results,
                                  reason=str(exc), next_step=i)
            step_results.append(fn(**step.get("args", {})))
        self._cps.put(task.id, {"step": len(task.steps), "results": step_results},
                      variant_id=VARIANT_ID)
        return TaskResult(task.id, "completed", step_results)
