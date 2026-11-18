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
_CATEGORY_RANK = {
    ActionCategory.READ_DRAFT: 0,
    ActionCategory.WORKSPACE_WRITE: 1,
    ActionCategory.VISIBLE_WRITE: 2,
    ActionCategory.IRREVERSIBLE: 3,
}


@dataclass(frozen=True)
class ToolPolicy:
    """工具能力声明。安全级别由工具决定，模型只能加严不能降级。"""
    category: ActionCategory = ActionCategory.READ_DRAFT
    description: str = ""


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, tuple] = {}

    def register(self, name: str, fn: Callable, policy: ToolPolicy = None,
                 category: ActionCategory = None, description: str = "") -> None:
        if policy is None:
            policy = ToolPolicy(category=category or ActionCategory.READ_DRAFT,
                                description=description)
        self._tools[name] = (fn, policy)

    def get(self, name: str) -> Callable:
        if name not in self._tools:
            raise KeyError(f"unknown_tool:{name}")
        return self._tools[name][0]

    def policy(self, name: str) -> ToolPolicy:
        if name not in self._tools:
            raise KeyError(f"unknown_tool:{name}")
        return self._tools[name][1]

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

    def _tool_exec_audit(self, tool: str, category: ActionCategory,
                         path: str = None, error: str = None) -> None:
        """工具执行入审计链（payload 有界，防日志膨胀）。"""
        audit = getattr(self._kernel, "audit", None)
        if audit is None:
            return
        from proto.kernel.audit import AuditEvent
        payload = {"tool": tool, "category": category.value}
        if path is not None:
            payload["path"] = str(path)[:200]
        if error is not None:
            payload["error"] = error[:200]
        audit.append(AuditEvent.TOOL_EXEC, payload)

    @staticmethod
    def _higher_category(a: ActionCategory, b: ActionCategory) -> ActionCategory:
        return a if _CATEGORY_RANK[a] >= _CATEGORY_RANK[b] else b

    def _effective_category(self, step: dict) -> ActionCategory:
        tool = step.get("tool")
        try:
            policy = self._tools.policy(tool)
        except KeyError:
            if tool == "write_file":
                # Router 内建 scratch 写：未在注册表声明时按工作区写处理
                policy = ToolPolicy(category=ActionCategory.WORKSPACE_WRITE)
            else:
                raise
        requested = step.get("category")
        if requested:
            try:
                requested_category = ActionCategory[str(requested).upper()]
            except KeyError as exc:
                raise ValueError(f"非法动作分级: {requested!r}") from exc
            return self._higher_category(policy.category, requested_category)
        return policy.category

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
        pending_step = state.get("pending_step")
        if pending_step and pending_step.get("tool") != "write_file":
            try:
                result = self._tools.get(pending_step["tool"])(
                    **(pending_step.get("args", {}) or {}))
            except Exception as exc:
                return TaskResult(
                    task.id, "failed",
                    step_results=state.get("results", []) + [
                        f"工具执行失败: {type(exc).__name__}: {exc}"],
                    reason=f"{type(exc).__name__}: {exc}", next_step=step)
            step_results = state.get("results", []) + [result]
            return self._execute(task, start_step=step + 1,
                                 step_results=step_results)
        return self._execute(task, start_step=step + 1,
                             step_results=state.get("results", []))

    def _execute(self, task: Task, start_step: int, step_results: list) -> TaskResult:
        task_spec = {"description": task.description, "steps": task.steps,
                     "owner_assigned": task.owner_assigned,
                     "desire_dim": task.desire_dim}
        for i in range(start_step, len(task.steps)):
            step = task.steps[i]
            try:
                category = self._effective_category(step)
            except (KeyError, ValueError) as exc:
                return TaskResult(task.id, "failed", step_results,
                                  reason=str(exc), next_step=i)
            self._cps.put(task.id, {
                "step": i, "results": step_results, "task": task_spec,
            }, variant_id=VARIANT_ID)
            if step["tool"] == "write_file":
                args = step.get("args", {}) or {}
                path = args.get("path") or args.get("file_path") or args.get("filename")
                if not path:
                    step_results.append(
                        f"错误：write_file 需要 path 参数（收到键：{list(args)}）")
                    continue
                if (category is ActionCategory.WORKSPACE_WRITE
                        and self._workspace is not None):
                    self._workspace.write_file(
                        path, step["args"].get("content", ""))
                    self._tool_exec_audit(step["tool"], category, path)
                    step_results.append(f"written:{path}")
                    continue
                self._ws.write(path, step["args"].get("content", ""))
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
                            "pending_step": step,
                        }, variant_id=VARIANT_ID)
                        return TaskResult(task.id, "paused", step_results,
                                          proposal_id=sub.proposal_id, next_step=i)
                    if sub.auto_approved and sub.approval is not None:
                        self._ws.promote(path, approval=sub.approval)
                step_results.append(f"written:{path}")
                continue
            try:
                fn = self._tools.get(step["tool"])
            except KeyError as exc:
                return TaskResult(task.id, "failed", step_results,
                                  reason=str(exc), next_step=i)
            if category in (ActionCategory.VISIBLE_WRITE,
                            ActionCategory.IRREVERSIBLE):
                sub = self._queue.submit(ActionProposal(
                    category, f"{step['tool']}（{task.description}）",
                    {"step": step, "step_index": i}, task_id=task.id,
                    self_initiated=not task.owner_assigned,
                    desire_dim=task.desire_dim))
                if sub.rejected:
                    return TaskResult(task.id, "denied", step_results,
                                      reason=sub.reason, next_step=i)
                if not sub.auto_approved:
                    self._cps.put(task.id, {
                        "step": i, "results": step_results, "task": task_spec,
                        "waiting_for": sub.proposal_id,
                        "pending_step": step,
                    }, variant_id=VARIANT_ID)
                    return TaskResult(task.id, "paused", step_results,
                                      proposal_id=sub.proposal_id, next_step=i)
            # LLM 常带多余键：只传工具声明的参数（防御性过滤）
            import inspect as _insp
            try:
                sig_params = set(_insp.signature(fn).parameters)
            except (ValueError, TypeError):
                sig_params = None
            call_args = step.get("args", {}) or {}
            if sig_params is not None:
                extra = set(call_args) - sig_params
                if extra:
                    step_results.append(
                        f"忽略未知参数: {sorted(extra)}")
                    call_args = {k: v for k, v in call_args.items()
                                 if k in sig_params}
            try:
                outcome = fn(**call_args)
                step_results.append(outcome)
                self._tool_exec_audit(step["tool"], category, None)
            except Exception as exc:
                self._tool_exec_audit(step["tool"], category, None,
                                      error=f"{type(exc).__name__}: {exc}")
                return TaskResult(task.id, "failed", step_results,
                                  reason=f"{type(exc).__name__}: {exc}",
                                  next_step=i)
        self._cps.put(task.id, {"step": len(task.steps), "results": step_results},
                      variant_id=VARIANT_ID)
        return TaskResult(task.id, "completed", step_results)
