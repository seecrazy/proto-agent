# -*- coding: utf-8 -*-
"""M4-②: Router/Worker 执行器骨架（设计 §8）。

Router 串行调度任务：预算预留 → 逐步执行 → 每步检查点 → 对外动作走审批
（暂停于检查点，批准恢复 / 拒绝中止且产物留在 scratch）。
步骤的产生者后续由 LLM 策略接管（决定调用什么工具），安全管线不变。
"""
from __future__ import annotations

import threading
from contextlib import nullcontext
import copy
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from proto.exec.approval import ActionProposal, ApprovalQueue
from proto.exec.checkpoints import CheckpointStore, SCHEMA_VERSION
from proto.exec.contracts import (ParameterError, builtin_parameters, check_schema,
                                  checked_arguments, infer_parameters)
from proto.exec.workspace import ScratchWorkspace
from proto.kernel.kernel import ActionCategory, Kernel
from proto.runtime.runcontext import track_checkpoint

DEFAULT_STEP_COST = {"tokens": 20.0}
VARIANT_ID = "core@0.3.0"
# 未声明观察限额的工具，观察文本默认截断长度（M47）
OBS_DEFAULT = 600
_CATEGORY_RANK = {
    ActionCategory.READ_DRAFT: 0,
    ActionCategory.WORKSPACE_WRITE: 1,
    ActionCategory.VISIBLE_WRITE: 2,
    ActionCategory.IRREVERSIBLE: 3,
}


@dataclass(frozen=True)
class ToolPolicy:
    """工具能力声明。安全级别由工具决定，模型只能加严不能降级。
    owner_only=True 的工具属"较高权限"：主人超时也绝不自主代批。"""
    category: ActionCategory = ActionCategory.READ_DRAFT
    description: str = ""
    owner_only: bool = False


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, tuple] = {}
        self._parameters: Dict[str, dict] = {}
        self._read_only: set = set()
        self._obs_limits: Dict[str, int] = {}
        self._background_io: set = set()

    def register(self, name: str, fn: Callable, policy: ToolPolicy = None,
                 category: ActionCategory = None, description: str = "",
                 owner_only: bool = False, parameters: dict = None,
                 read_only: bool = False, obs_limit: int = None,
                 background_io: bool = False) -> None:
        if policy is None:
            policy = ToolPolicy(category=category or ActionCategory.READ_DRAFT,
                                description=description, owner_only=owner_only)
        schema = copy.deepcopy(parameters) if parameters is not None else infer_parameters(fn)
        check_schema(schema)
        if schema.get("type") != "object":
            raise ValueError("tool_parameters_must_be_object")
        self._tools[name] = (fn, policy)
        self._parameters[name] = schema
        # 行为元数据（只读探索计数 / 观察截断 / 后台 IO）：重复注册按最新声明覆盖
        if read_only:
            self._read_only.add(name)
        else:
            self._read_only.discard(name)
        if obs_limit is not None:
            self._obs_limits[name] = int(obs_limit)
        else:
            self._obs_limits.pop(name, None)
        if background_io:
            self._background_io.add(name)
        else:
            self._background_io.discard(name)

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

    def read_only_tools(self) -> set:
        """只读工具集合（探索步数配额计数用）。"""
        return set(self._read_only)

    def obs_limit(self, name: str) -> int:
        """观察文本截断限额；未声明的工具取 OBS_DEFAULT。"""
        return self._obs_limits.get(name, OBS_DEFAULT)

    def is_background_io(self, name: str) -> bool:
        """耗时 I/O 工具：执行时切后台，避免阻塞前台对话。"""
        return name in self._background_io

    def definition(self, name: str) -> dict:
        policy = self.policy(name)
        return {"name": name, "description": policy.description,
                "category": policy.category.value, "owner_only": policy.owner_only,
                "parameters": copy.deepcopy(self._parameters[name])}

    def definitions(self) -> List[dict]:
        return [self.definition(name) for name in sorted(self.names())]

    def validate(self, name: str, arguments: dict) -> dict:
        self.get(name)
        return checked_arguments(name, arguments, self._parameters[name])


@dataclass
class Task:
    id: str
    description: str
    steps: List[dict]  # {"tool", "args", "category"}
    owner_assigned: bool = True
    desire_dim: Optional[str] = None  # 自主任务关联的欲望维度（阻尼/预算语义）
    autonomous: bool = False


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
                 step_cost: Dict[str, float] = None, code_workspace=None, autonomy_check=None, tool_io=None):
        self._kernel = kernel
        self._queue = queue
        self._ws = workspace
        self._workspace = code_workspace   # CodeTools：工作区写自动放行
        self._cps = checkpoints
        self._tools = tools
        self._cost = step_cost or DEFAULT_STEP_COST
        self._resume_lock = threading.RLock()
        self._autonomy_check = autonomy_check
        self._tool_io = tool_io or (lambda tool: nullcontext())

    def _pool(self, task: Task) -> str:
        return "owner_tasks" if task.owner_assigned else "self_initiated"

    def _blocked(self, task: Task, step_results=None, step: int = 0) -> Optional[TaskResult]:
        if self._autonomy_check is not None:
            reason = self._autonomy_check(task.autonomous or not task.owner_assigned)
            if reason:
                return TaskResult(task.id, "blocked", step_results or [], reason=reason, next_step=step)
        return None

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
        track_checkpoint(task.id)
        blocked = self._blocked(task)
        if blocked:
            return blocked
        total = {unit: amount * len(task.steps) for unit, amount in self._cost.items()}
        if not self._kernel.pools.try_consume(self._pool(task), total).granted:
            return TaskResult(task.id, "no_budget", reason="budget_pool_exhausted")
        return self._execute(task, start_step=0, step_results=[])

    def resume(self, task: Task, human_approved: bool) -> TaskResult:
        """从暂停检查点恢复：批准→继续余下步骤；拒绝→中止（产物留 scratch）。"""
        if type(human_approved) is not bool:
            return TaskResult(task.id, "failed", reason="invalid_approval_decision")
        track_checkpoint(task.id)
        with self._resume_lock:
            return self._resume_locked(task, human_approved)

    def _resume_locked(self, task: Task, human_approved: bool) -> TaskResult:
        record = self._cps.resume(task.id)
        if record is None:
            return TaskResult(task.id, "failed", reason="no_checkpoint")
        if (type(record.get("schema_version")) is not int or record["schema_version"] != SCHEMA_VERSION
                or record.get("variant_id") != VARIANT_ID):
            return TaskResult(task.id, "failed", reason="checkpoint_version_mismatch")
        state = record["state"]
        if not isinstance(state, dict):
            return TaskResult(task.id, "failed", reason="invalid_checkpoint")
        proposal_id = state.get("waiting_for")
        if proposal_id is None:
            return TaskResult(task.id, "failed", reason="not_paused")
        pending_step = state.get("pending_step")
        if human_approved:
            spec = state.get("task")
            step_index = state.get("step")
            if (not isinstance(spec, dict) or not isinstance(spec.get("steps"), list)
                    or not isinstance(spec.get("description"), str)
                    or not isinstance(state.get("results"), list)
                    or type(step_index) is not int or not 0 <= step_index < len(spec["steps"])
                    or not isinstance(pending_step, dict) or pending_step != spec["steps"][step_index]):
                return TaskResult(task.id, "failed", reason="invalid_checkpoint")
            try:
                current_category = self._effective_category(pending_step)
                tool = pending_step["tool"]
                policy = self._tools.policy(tool) if tool in self._tools.names() else None
                if (state.get("approved_category", current_category.value) != current_category.value
                        or state.get("owner_only", bool(policy and policy.owner_only)) != bool(policy and policy.owner_only)):
                    return TaskResult(task.id, "failed", reason="tool_policy_changed")
                schema = (self._tools._parameters[tool] if tool in self._tools.names()
                          else builtin_parameters("write_file"))
                checked_arguments(tool, pending_step.get("args", {}), schema)
            except (KeyError, ValueError) as exc:
                return TaskResult(task.id, "failed", reason=f"parameter_contract:{exc}")
            task = Task(task.id, spec["description"], spec["steps"],
                        owner_assigned=spec.get("owner_assigned", True),
                        desire_dim=spec.get("desire_dim"),
                        autonomous=task.autonomous or spec.get("autonomous") is True)
            blocked = self._blocked(task, state["results"], step_index)
            if blocked:
                return blocked
        try:
            approval = (self._queue.approve(proposal_id) if human_approved else
                        self._queue.deny(proposal_id, reason="human_denied"))
        except KeyError:
            return TaskResult(task.id, "failed", reason="approval_not_found")
        claimed = {key: value for key, value in state.items()
                   if key not in ("waiting_for", "pending_step", "pending_promotion")}
        claimed["status"] = ("resuming" if human_approved and approval.granted
                             else "denied")
        self._cps.put(task.id, claimed, variant_id=record["variant_id"])
        if not human_approved or not approval.granted:
            return TaskResult(task.id, "denied", step_results=state.get("results", []),
                              reason=approval.reason or "human_denied")
        return self._execute(task, start_step=state["step"],
                             step_results=state.get("results", []),
                             approved_step=state["step"], approval=approval)

    def _execute(self, task: Task, start_step: int, step_results: list,
                 approved_step: int = None, approval=None) -> TaskResult:
        task_spec = {"description": task.description, "steps": copy.deepcopy(task.steps),
                     "owner_assigned": task.owner_assigned,
                     "desire_dim": task.desire_dim, "autonomous": task.autonomous}
        for i in range(start_step, len(task.steps)):
            blocked = self._blocked(task, step_results, i)
            if blocked:
                return blocked
            step = task.steps[i]
            if not isinstance(step, dict) or not isinstance(step.get("args", {}), dict):
                step_results.append("参数错误：工具步骤和 args 必须是对象。修正后重试。")
                continue
            tool_policy = None
            try:
                category = self._effective_category(step)
                tool = step.get("tool")
                if tool in self._tools.names():
                    tool_policy = self._tools.policy(tool)
            except (KeyError, ValueError) as exc:
                return TaskResult(task.id, "failed", step_results,
                                  reason=str(exc), next_step=i)
            try:
                schema = (self._tools._parameters[step["tool"]]
                          if step["tool"] in self._tools.names()
                          else builtin_parameters("write_file"))
                step = dict(step, args=checked_arguments(step["tool"], step.get("args", {}), schema))
                task_spec["steps"][i] = step
            except ParameterError as exc:
                step_results.append(f"参数错误：{step['tool']} {exc}。修正参数后重试。")
                self._tool_exec_audit(step["tool"], category, error=f"parameter_contract:{exc}")
                continue
            self._cps.put(task.id, {
                "step": i, "results": step_results, "task": task_spec,
                "status": "resuming" if i == approved_step else "running",
            }, variant_id=VARIANT_ID)
            blocked = self._blocked(task, step_results, i)
            if blocked:
                return blocked
            if step["tool"] == "write_file":
                args = step.get("args", {}) or {}
                path = args.get("path") or args.get("file_path") or args.get("filename")
                if not path:
                    step_results.append(
                        f"错误：write_file 需要 path 参数（收到键：{list(args)}）")
                    continue
                if (category is ActionCategory.WORKSPACE_WRITE
                        and self._workspace is not None):
                    try:
                        with self._tool_io(step["tool"]):
                            outcome = self._workspace.write_file(path, args.get("content", ""))
                        self._tool_exec_audit(step["tool"], category, path)
                        step_results.append(outcome)
                    except Exception as exc:
                        self._tool_exec_audit(step["tool"], category, str(path), str(exc))
                        step_results.append(f"write_file 执行失败：{type(exc).__name__}: {exc}")
                    continue
                try:
                    with self._tool_io(step["tool"]):
                        self._ws.write(path, args.get("content", ""))
                except Exception as exc:
                    self._tool_exec_audit(step["tool"], category, str(path), str(exc))
                    step_results.append(f"write_file 执行失败：{type(exc).__name__}: {exc}")
                    continue
                blocked = self._blocked(task, step_results, i)
                if blocked:
                    return blocked
                if i == approved_step:
                    self._ws.promote(path, approval=approval)
                elif category in (ActionCategory.VISIBLE_WRITE, ActionCategory.IRREVERSIBLE):
                    sub = self._queue.submit(ActionProposal(
                        category, f"晋升 {path}（{task.description}）",
                        {"path": path}, task_id=task.id,
                        self_initiated=not task.owner_assigned,
                        desire_dim=task.desire_dim,
                        owner_only=bool(tool_policy and tool_policy.owner_only)))
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
                            "approved_category": category.value,
                            "owner_only": bool(tool_policy and tool_policy.owner_only),
                        }, variant_id=VARIANT_ID)
                        return TaskResult(task.id, "paused", step_results,
                                          proposal_id=sub.proposal_id, next_step=i)
                    if sub.auto_approved and sub.approval is not None:
                        blocked = self._blocked(task, step_results, i)
                        if blocked:
                            return blocked
                        self._ws.promote(path, approval=sub.approval)
                self._tool_exec_audit(step["tool"], category, str(path))
                step_results.append(f"written:{path}")
                continue
            try:
                fn = self._tools.get(step["tool"])
            except KeyError as exc:
                return TaskResult(task.id, "failed", step_results,
                                  reason=str(exc), next_step=i)
            if (i != approved_step and category in (
                    ActionCategory.VISIBLE_WRITE, ActionCategory.IRREVERSIBLE)):
                sub = self._queue.submit(ActionProposal(
                    category, f"{step['tool']}（{task.description}）",
                    {"step": step, "step_index": i}, task_id=task.id,
                    self_initiated=not task.owner_assigned,
                    desire_dim=task.desire_dim,
                    owner_only=bool(tool_policy and tool_policy.owner_only)))
                if sub.rejected:
                    return TaskResult(task.id, "denied", step_results,
                                      reason=sub.reason, next_step=i)
                if not sub.auto_approved:
                    self._cps.put(task.id, {
                        "step": i, "results": step_results, "task": task_spec,
                        "waiting_for": sub.proposal_id,
                        "pending_step": step,
                        "approved_category": category.value,
                        "owner_only": bool(tool_policy and tool_policy.owner_only),
                    }, variant_id=VARIANT_ID)
                    return TaskResult(task.id, "paused", step_results,
                                      proposal_id=sub.proposal_id, next_step=i)
            call_args = step.get("args", {}) or {}
            blocked = self._blocked(task, step_results, i)
            if blocked:
                return blocked
            try:
                with self._tool_io(step["tool"]):
                    outcome = fn(**call_args)
                step_results.append(outcome)
                self._tool_exec_audit(step["tool"], category, None)
            except TypeError as exc:
                # 参数错误=单步失败不是任务失败（M48）：给出可自纠的提示，
                # 链继续——原来 "TypeError: <lambda>() missing..." 直接判死整链
                hint = f"参数错误：{step['tool']}（{type(exc).__name__}: {exc}）"
                step_results.append(
                    hint + f"；收到参数 {sorted(call_args)}。修正参数后重试。")
                self._tool_exec_audit(step["tool"], category, None,
                                      error=f"arg_error:{exc}")
            except Exception as exc:
                # 工具执行失败同样是单步失败（M48）：观察带回错误，链继续，
                # 模型下一轮换路——任务级 failed 只留给系统性问题（未知工具/预算）
                self._tool_exec_audit(step["tool"], category, None,
                                      error=f"{type(exc).__name__}: {exc}")
                step_results.append(
                    f"{step['tool']} 执行失败：{type(exc).__name__}: {exc}"
                    "（换一种做法或修正后重试）")
        self._cps.put(task.id, {"step": len(task.steps), "results": step_results,
                               "status": "completed", "task": task_spec},
                      variant_id=VARIANT_ID)
        return TaskResult(task.id, "completed", step_results)
