"""Bounded idea schemas and structured semantic distance."""
from __future__ import annotations

import itertools
import json
import math
import re
from collections import Counter


FIELDS = ("core_insight", "target_context", "mechanism", "why_existing_fails",
          "failure_condition", "first_concrete_action")
OPERATORS = {"Op_HV": "HV", "Op_WH": "WH", "Op_VW": "VW", "Op_WHV": "WHV"}
ELEMENTS = {"W": "what", "H": "how", "V": "value"}
POSTURES = ("EXPLORE", "EXPLOIT", "FINAL_GAMBLE")
DEFAULT_DISTANCE = 0.80
EMBEDDING_VERSION = "structured-v1"
AXES = {"method": 0.50, "problem": 0.20, "value": 0.20, "actor": 0.10}
LABELS = {
    "method": {
        "snapshot": "保存状态、交接和恢复", "reconcile": "对账、合并冲突或去重", "receipt": "留痕与可查证回执",
        "simulate": "模拟或原型", "compare": "横评或实验对照", "recommend": "选择、排序和推荐",
        "route": "分发和委派", "batch": "合并处理或聚合提醒", "observe": "遥测与被动观察",
        "guard": "前置门槛和安全默认", "rollback": "反悔、撤销和可逆试行", "coach": "训练、提示和反馈",
        "retrieve": "查找和发现相关信息", "compose": "组合现有构件", "automate": "替代重复手工步骤",
    },
    "problem": {
        "lost_context": "上下文丢失", "repeat_work": "重复劳动", "hidden_state": "状态不透明",
        "false_success": "虚假完成", "handoff": "交接断层", "input_friction": "大量手工录入",
        "bad_choice": "缺少比较依据", "unproven_need": "需求未证实", "interruption": "中断或阻塞",
        "noise": "注意力噪声", "inconsistent_data": "记录矛盾", "irreversible": "操作难以反悔",
        "learning_gap": "理解或技能不足", "coordination": "协作碰撞",
    },
    "value": {
        "continuity": "连续性", "trust": "可复核与信任", "speed": "操作时间缩短", "focus": "少打扰",
        "adoption": "降低上手门槛", "reliability": "稳定完成", "cost": "资源更省", "learning": "学到新信息",
        "control": "可控可反悔", "coordination": "协作顺畅",
    },
    "actor": {
        "builder": "开发或制作", "operator": "工作运行者", "reviewer": "评审或审批", "learner": "学习者",
        "coordinator": "负责人或协调者", "consumer": "日常使用者",
    },
}

_EVIDENCE = re.compile(r"运行|测试|编译|退出码|断言|输出|复现|对照|测量|试验|脚本|回执|\b(test|assert|exit|run|build)\b", re.I)
_CAUSE = re.compile(r"因为|从而|因此|通过|如果|就能|降低|减少|使|让|避免|即可|\b(by|because|so that|when|lets|makes|reduces|enables|prevents)\b", re.I)
_BOUNDARY = re.compile(r"[。！？!?]+|\.(?=\s|$)")


def read_json(raw):
    if not isinstance(raw, str) or len(raw) > 48_000:
        raise ValueError("invalid_response_size")
    clean = raw.strip()
    if clean.startswith("```") and clean.endswith("```"):
        clean = re.sub(r"^```(?:json)?\s*|\s*```$", "", clean, flags=re.I)
    try:
        return json.loads(clean)
    except (ValueError, RecursionError):
        raise ValueError("invalid_json") from None


def text(value, limit):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise ValueError("invalid_text")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("invalid_text_encoding") from None
    return value.strip()


def materials(rows, evidence):
    if not isinstance(rows, list) or not 10 <= len(rows) <= 12:
        raise ValueError("material_count")
    result = {}
    counts = Counter()
    seen = set()
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or row.get("id") != f"M{index}":
            raise ValueError("material_id")
        source = row.get("source")
        if source not in ("agent", "user", "inertia"):
            raise ValueError("material_source")
        counts[source] += 1
        body = text(row.get("text"), 180)
        if body in seen:
            raise ValueError("duplicate_material")
        seen.add(body)
        item = {"id": row["id"], "source": source, "text": body, "basis": "hypothesis"}
        spans = set()
        for axis in ELEMENTS.values():
            span = text(row.get(axis), 100)
            if span not in body or span in spans or row.get(axis) != span:
                raise ValueError("material_not_verbatim")
            spans.add(span)
            item[axis] = span
        if source == "user":
            item.update(actor=text(row.get("actor"), 60), scene=text(row.get("scene"), 100))
            if row.get("evidence_ref") not in (None, ""):
                raise ValueError("user_hypothesis_not_observation")
        else:
            ref = row.get("evidence_ref")
            if ref not in (None, ""):
                if not isinstance(ref, str) or ref not in evidence:
                    raise ValueError("unknown_material_evidence")
                item.update(basis="observation", evidence_ref=ref)
        result[item["id"]] = item
    if counts["agent"] != 4 or counts["user"] != 4 or not 2 <= counts["inertia"] <= 4:
        raise ValueError("material_source_count")
    return result


def candidate(row, source_materials, idea_id):
    if not isinstance(row, dict):
        raise ValueError("invalid_candidate")
    result = {key: text(row.get(key), 196 if key == "core_insight" else 240) for key in FIELDS}
    insight = result["core_insight"]
    if "\n" in insight or len([part for part in _BOUNDARY.split(insight) if part.strip()]) != 1 or not _CAUSE.search(insight):
        raise ValueError("insight_not_causal_sentence")
    if any(normalized(insight) == normalized(item["text"]) for item in source_materials.values()):
        raise ValueError("insight_copied_from_material")
    inputs = row.get("required_inputs")
    if not isinstance(inputs, list) or len(inputs) > 2:
        raise ValueError("required_inputs")
    inputs = [text(item, 48) for item in inputs]
    if len(set(item.casefold() for item in inputs)) != len(inputs):
        raise ValueError("required_inputs")
    acceptance = text(row.get("acceptance"), 260)
    if not _EVIDENCE.search(acceptance) or not _EVIDENCE.search(result["first_concrete_action"]):
        raise ValueError("no_local_evidence")
    signature = row.get("signature")
    if not isinstance(signature, dict) or set(signature) != set(AXES) or any(
            not isinstance(signature[key], str) or signature[key] not in labels
            for key, labels in LABELS.items()):
        raise ValueError("unknown_semantic_label")
    operator = row.get("operator")
    refs = row.get("elements")
    if not isinstance(operator, str) or operator not in OPERATORS or not isinstance(refs, dict) or set(refs) != set(OPERATORS[operator]):
        raise ValueError("invalid_operator_elements")
    if any(not isinstance(ref, str) or ref not in source_materials for ref in refs.values()) or len(set(refs.values())) < 2:
        raise ValueError("no_cross_material_recombination")
    bounds = row.get("outcome_range")
    if not isinstance(bounds, list) or len(bounds) != 2 or any(type(value) is not int for value in bounds) or not 0 <= bounds[0] <= bounds[1] <= 4:
        raise ValueError("invalid_outcome_range")
    cited = [source_materials[ref] for ref in set(refs.values())]
    grounding = sum(item["basis"] == "observation" for item in cited) / len(cited)
    return dict(result, id=idea_id, signature=signature.copy(), operator=operator, elements=refs.copy(), grounding=grounding,
                required_inputs=inputs, acceptance=acceptance, outcome_range=bounds.copy())


def embedding(idea):
    return {f"{axis}:{idea['signature'][axis]}": math.sqrt(weight) for axis, weight in AXES.items()}


def distance(left, right):
    vector_left, vector_right = embedding(left), embedding(right)
    similarity = math.fsum(value * vector_right.get(key, 0.0) for key, value in vector_left.items())
    return round(1.0 - min(1.0, similarity), 12)


def normalized(text_value):
    return "".join(char for char in text_value.casefold() if char.isalnum())


def duplicate(left, right, threshold):
    return (normalized(left["core_insight"]) == normalized(right["core_insight"])
            or distance(left, right) < threshold)


def diverse_set(ideas, threshold, posture, quality):
    """Exact maximum-cardinality shortlist; the bounded input never exceeds 12 ideas."""
    def conflict(left, right):
        return (duplicate(left, right, threshold) or
                (posture == "EXPLORE" and left["signature"]["method"] == right["signature"]["method"]))

    conflicts = {frozenset((left["id"], right["id"]))
                 for left, right in itertools.combinations(ideas, 2) if conflict(left, right)}
    for size in range(min(5, len(ideas)), 0, -1):
        feasible = (group for group in itertools.combinations(ideas, size)
                    if all(frozenset((left["id"], right["id"])) not in conflicts
                           for left, right in itertools.combinations(group, 2)))
        best = max(feasible, key=lambda group: sum(quality(item) for item in group), default=None)
        if best is not None:
            return list(best), conflicts
    return [], conflicts
