# -*- coding: utf-8 -*-
"""GoalForge：把专家人设、主人优先级和学习记忆变成可交付目标。

这不是又一套队列——GoalTree 仍是目标唯一权威。GoalForge 只做三件事：
1. 从真实上下文生成候选想法（外部信号/反思/世界模型，不从零凭空想）；
2. 用便宜、确定性的本地评分淘汰跑偏、重复、无法本机验证的想法；
3. 把幸存想法播种为 source=self 的长期目标并分解出交付阶梯。
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import List, Optional


FORGE_PROMPT = (
    "你是 PROTO 的 GoalForge。请基于人设、主人优先级、审美、已有目标、"
    "近期反思和世界模型事实，提出 3-5 个值得现在做的候选想法。\n"
    "每个想法必须像一个高级专家会主动立项的任务，而不是“了解一下/调研一下”。\n"
    "必须全部可以在本机工作区设计、实现、测试并整理到可交付状态；"
    "如果最终发布需要主人账号/设备/付费，只能作为交付清单，不能作为完成标准。\n"
    '只输出 JSON 数组：[{"text":"目标","rationale":"为什么值得做",'
    '"feasibility":0.0~1.0,"acceptance":"≤3 条本机可检验证据"}]。'
)

_LOCAL_EVIDENCE = re.compile(
    r"存在|写出?|运行|执行|测试|编译|退出码|输出|文件|报告|清单|通过|覆盖"
)


def _tokens(text: str) -> set:
    words = set(re.findall(r"[a-zA-Z0-9]+", (text or "").lower()))
    words.update(ch for ch in (text or "") if "\u4e00" <= ch <= "\u9fff")
    return words


def _jaccard(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class GoalCandidate:
    text: str
    rationale: str
    feasibility: float
    acceptance: str
    score: float = 0.0
    breakdown: Optional[dict] = None


class GoalForge:
    """候选生成与本地淘汰。持久状态只记录节奏/统计，不保存目标本体。"""

    MAX_ACTIVE_LONG = 3
    DRIVE_INTERVAL_S = 900.0

    def __init__(self, store, clock):
        self._store = store
        self._clock = clock
        raw = store.get("goal_forge", "main") or {}
        self.last_attempt_ts = float(raw.get("last_attempt_ts", 0.0))
        self.last_created_ts = float(raw.get("last_created_ts", 0.0))
        self.attempts = int(raw.get("attempts", 0))
        self.created = int(raw.get("created", 0))
        self.last_error = str(raw.get("last_error", ""))[:200]

    def _save(self, error: str = "") -> None:
        self.last_error = error[:200]
        self._store.put("goal_forge", "main", {
            "last_attempt_ts": self.last_attempt_ts,
            "last_created_ts": self.last_created_ts,
            "attempts": self.attempts,
            "created": self.created,
            "last_error": self.last_error,
        })

    @staticmethod
    def _fallback_ladder(goal_tree, node) -> List[object]:
        """LLM 分解失败也不让愿景悬空：确定性交付阶梯兜底。"""
        stages = (
            (f"领域学习与设计规格：{node.text}",
             "存在设计文档，包含优秀同类作品/模式的学习要点、用户价值与取舍"),
            (f"功能实现：{node.text}",
             "项目目录存在实现文件，且不是空壳/占位内容"),
            (f"本地验证：{node.text}",
             "存在可重复运行的测试或编译命令，关键路径退出码为 0"),
            ("整理就绪交付包：README、运行/测试证据、待主人提供事项、一键继续指引",
             "项目目录存在 README 与交付报告；报告列出本机证据、待主人提供事项和一键继续指引"),
        )
        return [goal_tree.add(text, "mid", parent_id=node.id,
                              source=node.source, acceptance=acceptance)
                for text, acceptance in stages]

    def _context(self, agent) -> str:
        reflections = [m.text for m in agent.memory.reflections()[-3:]]
        facts = []
        for topic in list(agent.world.snapshot()["topics"].values())[-3:]:
            facts.extend(f["text"] for f in topic.get("facts", [])[-2:])
        active = [n.text for n in agent.goal_tree.all_active()]
        taste = load_taste_text(agent)
        return json.dumps({
            "persona": agent.persona[:300],
            "owner_priority": (agent.state.owner_priority or "")[:200],
            "active_goals": active[:12],
            "reflections": reflections,
            "world_facts": facts[-6:],
            "taste_anchors": taste,
        }, ensure_ascii=False, indent=2)

    @staticmethod
    def _parse(raw: str) -> List[GoalCandidate]:
        cleaned = re.sub(r"```(?:json)?", "", (raw or "").strip())
        start, end = cleaned.find("["), cleaned.rfind("]")
        if start == -1 or end <= start:
            raise ValueError("json_array_not_found")
        rows = json.loads(cleaned[start:end + 1])
        out = []
        for row in rows[:5]:
            if not isinstance(row, dict):
                continue
            if not isinstance(row.get("text"), str) or not isinstance(row.get("acceptance"), str):
                continue
            text = row["text"].strip()
            acceptance = row["acceptance"].strip()
            if not text or not acceptance:
                continue
            try:
                feasibility = float(row.get("feasibility", 0.65))
                feasibility = (max(0.0, min(1.0, feasibility))
                               if math.isfinite(feasibility) else 0.65)
            except (TypeError, ValueError):
                feasibility = 0.65
            out.append(GoalCandidate(
                text=text[:200], rationale=str(row.get("rationale", ""))[:300],
                feasibility=feasibility, acceptance=acceptance[:300]))
        if not out:
            raise ValueError("empty_candidates")
        return out

    @classmethod
    def _local_score(cls, c: GoalCandidate, persona: str, priority: str,
                     active_texts: List[str]) -> float:
        owner_alignment = _jaccard(c.text, priority) if priority.strip() else 0.45
        persona_fit = max(_jaccard(c.text, persona),
                          _jaccard(c.rationale, persona))
        novelty = 1.0 - max((_jaccard(c.text, t) for t in active_texts),
                            default=0.0)
        evidence = 1.0 if _LOCAL_EVIDENCE.search(c.acceptance) else 0.0
        breakdown = {
            "owner_alignment": round(owner_alignment, 4),
            "persona_fit": round(persona_fit, 4),
            "novelty": round(novelty, 4),
            "feasibility": round(c.feasibility, 4),
            "evidence": evidence,
        }
        score = (0.35 * owner_alignment + 0.25 * persona_fit
                 + 0.15 * novelty + 0.15 * c.feasibility + 0.10 * evidence)
        c.score, c.breakdown = round(score, 4), breakdown
        return score

    def maybe_create(self, agent, trigger: str = "schedule",
                     min_interval_s: Optional[float] = None):
        """生成并播种一个目标。失败持久限频，避免每拍心跳烧快模型。"""
        now = self._clock()
        interval = self.DRIVE_INTERVAL_S if min_interval_s is None else min_interval_s
        if trigger != "schedule" and now - self.last_attempt_ts < interval:
            return None
        self.last_attempt_ts = now
        self.attempts += 1
        if agent.llm_fast is None:
            self._save("no_llm")
            return None
        if len(agent.goal_tree.active_long_term()) >= self.MAX_ACTIVE_LONG:
            self._save()
            return None
        try:
            raw = agent._complete(agent.llm_fast, FORGE_PROMPT,
                                  self._context(agent), pool="self_initiated")
            candidates = self._parse(raw)
        except Exception as exc:
            self._save(f"{type(exc).__name__}:{exc}")
            agent.spine.append("goal_forge_failed", {
                "trigger": trigger, "error": self.last_error})
            return None

        active_texts = [n.text for n in agent.goal_tree.all_active()]
        ranked = []
        for c in candidates:
            if any(_jaccard(c.text, text) >= 0.85 for text in active_texts):
                continue
            score = self._local_score(c, agent.persona,
                                      agent.state.owner_priority or "", active_texts)
            if score >= 0.42 and _LOCAL_EVIDENCE.search(c.acceptance):
                ranked.append(c)
        ranked.sort(key=lambda c: c.score, reverse=True)
        winner = ranked[0] if ranked else None
        if winner is None:
            self._save("no_candidate_passed_local_gate")
            agent.spine.append("goal_forge_rejected", {
                "trigger": trigger, "candidates": len(candidates)})
            return None

        node = agent.goal_tree.add(winner.text, "long", source="self",
                                   acceptance=winner.acceptance)
        children = agent.goal_tree.decompose(
            node, agent.llm_fast,
            complete=lambda client, system, user: agent._complete(
                client, system, user, pool="self_initiated"))
        if not children:
            children = self._fallback_ladder(agent.goal_tree, node)
        self.last_created_ts = now
        self.created += 1
        self._save()
        agent.spine.append("goal_forge_created", {
            "goal_id": node.id, "goal": node.text[:120],
            "score": winner.score, "breakdown": winner.breakdown,
            "trigger": trigger, "children": len(children)})
        agent.memory.add("reflection",
                         f"GoalForge 立项：{node.text}。理由：{winner.rationale}",
                         importance=0.75, source="goal_forge")
        agent._inbox.append(
            f"〔自主立项〕{node.text}（评分 {winner.score:.2f}）"
            f"已分解为 {len(children)} 个交付阶段。")
        return node


def load_taste_text(agent) -> dict:
    """延迟读取审美锚点，返回维度→锚点。"""
    from proto.runtime.taste import load_taste
    return {k: d.get("anchor", "") for k, d in load_taste(agent.spine.store).items()}
