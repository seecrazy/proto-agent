# -*- coding: utf-8 -*-
"""M20-①: 目标树——L3 自主性的第一支柱。

长期→中期→短期→任务的持久层级；目标分解（decompose）是独立机制；
进度沿树向上传播；选优执行（对齐×深度×进度）；里程碑触发情绪与记忆。
目标来源区分 owner（主人播种）与 self（预测误差等自主生成）。
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from proto.storage.store import DocumentStore

LEVELS = ("long", "mid", "short", "task")
CHILD_LEVEL = {"long": "mid", "mid": "short", "short": "task"}
DEPTH_BONUS = {"long": 0.2, "mid": 0.5, "short": 0.8, "task": 1.0}

DECOMPOSE_PROMPT = (
    "你是 PROTO 的目标分解器。把下面的父目标拆成 2-3 个更具体的子目标"
    "（各自可独立验证完成）。只输出 JSON 数组：[{\"text\": \"...\"}]。"
)


def _tokens(text: str) -> set:
    toks = set(re.findall(r"[A-Za-z0-9]+", text.lower()))
    toks.update(ch for ch in text if "\u4e00" <= ch <= "\u9fff")
    return toks


def _alignment(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class GoalNode:
    id: str
    text: str
    level: str
    parent_id: Optional[str] = None
    status: str = "active"          # active | done | dropped
    progress: float = 0.0
    source: str = "owner"           # owner | self
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return vars(self)


class GoalTree:
    def __init__(self, store: DocumentStore, collection: str = "goaltree"):
        self._store = store
        self._collection = collection
        self.on_milestone = None   # 完成中期/长期目标时的回调（agent 注入情绪/记忆）
        self._nodes: Dict[str, GoalNode] = {
            n.id: n for n in (
                GoalNode(**{k: r[k] for k in
                            ("id", "text", "level", "parent_id", "status",
                             "progress", "source", "created_at")})
                for r in store.find(collection, lambda _d: True))}

    def add(self, text: str, level: str, parent_id: str = None,
            source: str = "owner") -> GoalNode:
        if level not in LEVELS:
            raise ValueError(f"非法层级: {level}")
        node = GoalNode(id=uuid.uuid4().hex[:12], text=text[:200], level=level,
                        parent_id=parent_id, source=source)
        self._nodes[node.id] = node
        self._store.put(self._collection, node.id, node.to_dict())
        return node

    def get(self, node_id: str) -> Optional[GoalNode]:
        return self._nodes.get(node_id)

    def all_active(self) -> List[GoalNode]:
        return [n for n in self._nodes.values() if n.status == "active"]

    def active_long_term(self) -> List[GoalNode]:
        return sorted((n for n in self.all_active() if n.level == "long"),
                      key=lambda n: n.created_at)

    def children_of_level(self, parent_id: str, level: str) -> List[GoalNode]:
        return [n for n in self.all_active()
                if n.parent_id == parent_id and n.level == level]

    def complete(self, node_id: str) -> Optional[GoalNode]:
        node = self._nodes.get(node_id)
        if node is None:
            return None
        node.status = "done"
        node.progress = 1.0
        self._store.put(self._collection, node.id, node.to_dict())
        if node.level in ("mid", "long") and self.on_milestone:
            self.on_milestone(node)
        self._repropagate(node.parent_id)
        return node

    def drop(self, node_id: str) -> None:
        node = self._nodes.get(node_id)
        if node is None:
            return
        node.status = "dropped"
        self._store.put(self._collection, node.id, node.to_dict())
        self._repropagate(node.parent_id)

    def _repropagate(self, parent_id: Optional[str]) -> None:
        if not parent_id:
            return
        parent = self._nodes.get(parent_id)
        if parent is None:
            return
        kids = [n for n in self._nodes.values() if n.parent_id == parent_id]
        if kids:
            parent.progress = round(
                sum(k.progress for k in kids) / len(kids), 4)
        done = all(k.status == "done" for k in kids) if kids else False
        if done and parent.status == "active":
            parent.status = "done"
            parent.progress = 1.0
        self._store.put(self._collection, parent.id, parent.to_dict())
        self._repropagate(parent.parent_id)      # 逐级上传

    def select_executable(self, owner_priority: str) -> Optional[GoalNode]:
        """选当前最值得执行的目标：0.4×主人对齐 + 0.4×深度 + 0.2×进度。
        对齐度沿父链继承——子目标语义从属于父（词面重叠看不见），取整链最大值。"""
        priority = owner_priority.strip()
        if not priority:
            # 无优先级时退化为深度偏好（越具体越可执行）
            return max(self.all_active(),
                       key=lambda n: DEPTH_BONUS[n.level], default=None)

        def inherited_alignment(node: GoalNode) -> float:
            best = 0.0
            current = node
            while current is not None:
                best = max(best, _alignment(current.text, priority))
                current = self._nodes.get(current.parent_id) if current.parent_id else None
            return best

        best_node, best_score = None, -1.0
        for node in self.all_active():
            score = (0.4 * inherited_alignment(node)
                     + 0.4 * DEPTH_BONUS[node.level]
                     + 0.2 * node.progress)
            if score > best_score:
                best_node, best_score = node, score
        return best_node

    def decompose(self, node: GoalNode, llm) -> List[GoalNode]:
        """目标分解（独立于生成）：LLM 拆子目标；失败/离线返回空。"""
        child_level = CHILD_LEVEL.get(node.level)
        if child_level is None or llm is None:
            return []
        try:
            raw = llm.complete(DECOMPOSE_PROMPT, node.text)
            cleaned = re.sub(r"```(?:json)?", "", raw).strip()
            start, end = cleaned.find("["), cleaned.rfind("]")
            if start == -1:
                return []
            rows = __import__("json").loads(cleaned[start:end + 1])
        except Exception:
            return []
        children = []
        for row in rows[:3]:
            text = str(row.get("text", "")).strip()
            if text:
                children.append(self.add(text, child_level,
                                         parent_id=node.id, source=node.source))
        return children

    def seed_from_priority(self, owner_priority: str, llm) -> Optional[GoalNode]:
        """主人优先级 → 长期目标播种（幂等：已有活跃长期目标则跳过）。"""
        if not owner_priority.strip():
            return None
        existing = [n for n in self.active_long_term()
                    if _alignment(n.text, owner_priority) > 0.6]
        if existing:
            return existing[0]
        long_goal = self.add(owner_priority[:200], "long", source="owner")
        self.decompose(long_goal, llm)
        return long_goal

    def snapshot(self) -> dict:
        def node_view(n):
            return {"id": n.id, "text": n.text, "level": n.level,
                    "status": n.status, "progress": n.progress,
                    "source": n.source}
        return {"nodes": [node_view(n) for n in
                          sorted(self._nodes.values(), key=lambda x: x.created_at)]}
