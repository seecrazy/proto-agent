"""Read-only model planning and immutable owner approval over durable Runs."""
from __future__ import annotations

import hashlib
import json

from proto.exec.context import ContextError, create_context, render_context
from proto.exec.plan_evidence import (EvidenceError, OutputEvidence, valid_approved_plan, valid_output_paths,
                                      valid_path, valid_sha256, valid_steps, valid_text)
from proto.llm.client import LLMError
from proto.coordination.coordination import CoordinationError, encode


PLAN_EXEC_KEY = "plan-exec:"

_PLANNER = (
    "你负责为主人指派生成只读计划。现在没有工具，不执行任务，不声称已完成。"
    "保留原任务，列 1 到 6 个可执行步骤（每条最多 500 字），给出独立验收条件（最多 1000 字）。"
    "主人声明的输出另有硬验收，不自行增删。只返回 JSON 对象："
    '{"steps":["步骤"],"acceptance":"可核对的语义验收"}。'
)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_field")
        result[key] = value
    return result


def _parse_plan(raw):
    if not isinstance(raw, str) or len(raw) > 8192:
        raise ValueError("invalid_plan_response")
    try:
        result = json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError) as exc:
        raise ValueError("invalid_plan_response") from exc
    if (not isinstance(result, dict) or set(result) != {"steps", "acceptance"}
            or not valid_steps(result["steps"]) or not valid_text(result["acceptance"], 1000)):
        raise ValueError("invalid_plan_response")
    return {"steps": [step.strip() for step in result["steps"]], "acceptance": result["acceptance"].strip()}


def _digest(plan):
    return hashlib.sha256(encode(plan).encode("utf-8")).hexdigest()


def _rule_manifest(context):
    return [{"path": path, "sha256": context["instructions"][path]["sha256"]}
            for path in context["active_instructions"]]


def _reviewed(run, digest):
    if run["kind"] != "plan" or run["status"] != "succeeded":
        raise CoordinationError("plan_not_ready")
    result = run["result"] or {}
    plan = result.get("plan")
    if (not isinstance(plan, dict) or plan.get("id") != run["id"] or plan.get("agent_id") != run["agent_id"]
            or digest != result.get("sha256") or digest != _digest(plan)):
        raise CoordinationError("plan_digest_mismatch")
    return plan


class TaskPlans:
    def __init__(self, runs):
        self.runs = runs

    def enqueue(self, agent_id, text, project_dir, outputs, *, key=None):
        if project_dir == ".":
            project_dir = ""
        if not valid_text(text, 16000):
            raise CoordinationError("invalid_task_text", 400)
        if project_dir != "" and not valid_path(project_dir):
            raise CoordinationError("invalid_project_dir", 400)
        if not valid_output_paths(outputs):
            raise CoordinationError("invalid_output_paths", 400)
        return self.runs.enqueue(agent_id, "plan", {"text": text, "project_dir": project_dir, "outputs": outputs},
                                 key=key)

    def approve(self, plan_id, digest):
        if not valid_sha256(digest):
            raise CoordinationError("invalid_plan_digest", 400)
        with self.runs.database.connect(write=True) as connection:
            run = self.runs._get_locked(connection, plan_id)
            plan = _reviewed(run, digest)
            if self.runs.manager.info(plan["agent_id"]) is None:
                raise CoordinationError("no_such_agent", 404)
            child = self.runs.insert(connection, plan["agent_id"], "planned_task",
                                     {"plan_id": plan_id, "sha256": digest}, key=PLAN_EXEC_KEY + plan_id)
        self.runs.notify()
        return child

    def generate(self, run, agent, lease):
        args = run["args"]
        client = agent.llm_fast
        if client is None:
            return {"status": "no_llm", "reason": "planning_model_unavailable"}
        try:
            evidence = OutputEvidence(agent.code_workspace, args["project_dir"])
            evidence.require_project()
            outputs = evidence.snapshot(args["outputs"])
            context = create_context(agent, args["text"], "", "owner_tasks", project_dir=args["project_dir"])
        except (ContextError, EvidenceError) as exc:
            return {"status": "blocked", "reason": f"plan_context:{exc}"}
        try:
            prompt = render_context(context) + "\n主人声明的相对输出路径：" + encode(args["outputs"])
            raw = agent._complete(client, _PLANNER, prompt, pool="owner_tasks")
        except LLMError as exc:
            reason = lease.reason() or str(exc)
            status = "cancelled" if reason == "run_cancelled" else "failed"
            return {"status": status, "reason": reason}
        reason = lease.reason()
        if reason:
            return {"status": "cancelled", "reason": reason}
        try:
            proposal = _parse_plan(raw)
        except ValueError as exc:
            return {"status": "failed", "reason": str(exc)}
        plan = {"id": run["id"], "agent_id": run["agent_id"], "task": args["text"],
                "workspace": context["workspace"], "project_dir": args["project_dir"],
                "outputs": outputs, "rules": _rule_manifest(context), **proposal}
        return {"status": "completed", "plan": plan, "sha256": _digest(plan)}

    def execute(self, run, agent):
        args = run["args"]
        try:
            plan = _reviewed(self.runs.get(args["plan_id"]), args["sha256"])
            if plan["agent_id"] != run["agent_id"]:
                raise CoordinationError("plan_agent_mismatch")
            approved = {"id": plan["id"], "sha256": args["sha256"], "steps": plan["steps"],
                        "outputs": plan["outputs"]}
            if not valid_approved_plan(approved) or not valid_text(plan["acceptance"], 1000):
                raise CoordinationError("invalid_approved_plan")
            evidence = OutputEvidence(agent.code_workspace, plan["project_dir"])
            if str(evidence.root) != plan["workspace"]:
                raise EvidenceError("workspace_changed")
            evidence.require_project()
            context = create_context(agent, plan["task"], plan["acceptance"], "owner_tasks",
                                     project_dir=plan["project_dir"])
            if context["workspace"] != plan["workspace"] or _rule_manifest(context) != plan["rules"]:
                raise EvidenceError("plan_rules_changed")
            if evidence.snapshot([output["path"] for output in plan["outputs"]]) != plan["outputs"]:
                raise EvidenceError("plan_outputs_changed")
        except (ContextError, CoordinationError, EvidenceError) as exc:
            return {"status": "blocked", "reason": str(exc)}
        context["approved_plan"] = approved
        return agent.run_reviewed_task(context)
