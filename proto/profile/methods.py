# -*- coding: utf-8 -*-
"""M37: 方法策展——做事方法多元与自主发明。

用户核心诉求的机制化：
1. 人设与目标是"做什么/为什么"的核心；方法是"怎么做"的素材库；
2. 主人的方法提示是加分素材，永远不是方法的上限；
3. 除非主人显式限定（"只用/只能/仅限某方法"），每次推进目标的方法
   菜单必须包含自己发明的做法——想法设法是默认值，不是可选项；
4. 方法有使用/成败记账：好用的浮上来，久折的沉下去，没人用的靠
   新颖性加分冒头（探索-利用平衡）。

持久化：store["methods"]["main"] = {"methods": [...], "restriction": ...,
"last_invent_ts": ...}。零依赖，离线全降级。
"""
from __future__ import annotations

import time
import difflib
from dataclasses import dataclass, field
from typing import List, Optional

RESTRICT_MARKERS = ("只能用", "只能", "只用", "仅用", "仅限", "只准",
                    "限定方法", "限定", "不要用其他", "别用其他")


@dataclass
class MethodSpec:
    name: str            # 短 slug（菜单引用/战果归因用）
    description: str     # 一句话：怎么做、贴什么人设/目标
    source: str = "builtin"   # builtin | owner_hint | self_invented
    invented_at: float = 0.0
    used: int = 0
    wins: int = 0

    def to_dict(self) -> dict:
        return {"name": self.name, "description": self.description,
                "source": self.source, "invented_at": self.invented_at,
                "used": self.used, "wins": self.wins}

    @classmethod
    def from_dict(cls, raw: dict) -> "MethodSpec":
        return cls(name=str(raw.get("name", ""))[:40],
                   description=str(raw.get("description", ""))[:200],
                   source=raw.get("source", "builtin"),
                   invented_at=float(raw.get("invented_at", 0.0)),
                   used=int(raw.get("used", 0)),
                   wins=int(raw.get("wins", 0)))


# 内置起步方法：刻意多元（读代码/读文档/写原型/先测试/跑基准/重构/
# 写规格/外包/问主人/自查），避免"只会逛 GitHub"的单一性。
BUILTIN_METHODS = [
    ("github_scan", "浏览 GitHub：搜索/README/趋势，找参考实现与踩坑记录"),
    ("read_official_docs", "直接抓官方文档/Release Notes/迁移指南的 URL 精读"),
    ("write_prototype", "先写最小可运行原型（write_file+run_python 跑通）再迭代"),
    ("write_tests_first", "先把验收测试/用例写下来，跑红再实现到绿"),
    ("benchmark_compare", "写基准脚本对比 2-3 种实现/依赖，用数据选型"),
    ("teardown_rebuild", "读现有产物挑最差一块重构，不推倒重写"),
    ("micro_spec", "动手前写一页微型设计说明：取舍/风险/回滚路径"),
    ("delegate_heavy", "把大块体力活外包给外部编码 CLI（可用时）"),
    ("ask_owner_one_thing", "把唯一真正不确定的点浓缩成一句话问主人"),
    ("self_review_checklist", "产出后按自查清单逐项核验（能跑的跑起来验证）"),
    ("local_build_check", "用 run_python 调编译器做编译级检查：Swift 用 `xcrun swiftc -typecheck 文件`，产出零编译错误才算实现完成"),
]

_INVENT_PROMPT = (
    "你是 PROTO（人设：{persona}）。当前目标：{goal}\n"
    "主人语境（方法提示是素材，不是限制）：{context}\n"
    "已有方法库（不要重复）：{names}\n"
    '只输出 JSON：{{"owner_hinted": [{{"name": "slug", "description": "一句话"}}],'
    ' "invented": [{{"name": "slug", "description": "一句话"}}]}}\n'
    "要求：owner_hinted 只提炼主人语境里确实建议过的方法，没有就空数组；"
    "invented 发明 2 个库里没有、贴合人设与目标、现在就能执行的具体做法，"
    "要具体到用什么工具（读写工作区文件/run_python 跑代码/抓网页/外包 CLI）。"
)


def _tokens(text: str) -> set:
    import re
    return set(re.findall(r"[a-zA-Z_]{2,}|[\u4e00-\u9fff]", text.lower()))


class MethodLibrary:
    def __init__(self, store=None, now=time.time):
        self._store = store
        self._now = now
        self.methods: List[MethodSpec] = []
        self.restriction: Optional[str] = None      # 主人显式限定（原文）
        self.last_invent_ts: float = 0.0
        self._load()

    # ---- 持久化 ----
    def _load(self) -> None:
        if self._store is not None:
            raw = self._store.get("methods", "main") or {}
            self.methods = [MethodSpec.from_dict(r) for r in raw.get("methods", [])]
            self.restriction = raw.get("restriction")
            self.last_invent_ts = float(raw.get("last_invent_ts", 0.0))
        if not self.methods:                        # 首次播种内置方法
            self.methods = [MethodSpec(name=n, description=d,
                                       source="builtin", invented_at=self._now())
                            for n, d in BUILTIN_METHODS]
            self._save()

    def _save(self) -> None:
        if self._store is not None:
            self._store.put("methods", "main", {
                "methods": [m.to_dict() for m in self.methods],
                "restriction": self.restriction,
                "last_invent_ts": self.last_invent_ts,
            })

    # ---- 方法进出 ----
    def add(self, name: str, description: str, source: str) -> Optional[MethodSpec]:
        name = (name or "").strip().lower().replace(" ", "_")[:40]
        description = (description or "").strip()[:200]
        if not name or not description:
            return None
        # D3：同名/近似名都视为重复。LLM 常把 mine 写成 mining，
        # 仅 exact match 会每 15 分钟白烧一次发明调用。
        new_norm = name.replace("-", "_")
        for m in self.methods:
            old_norm = m.name.replace("-", "_")
            name_sim = difflib.SequenceMatcher(
                None, new_norm, old_norm).ratio()
            desc_sim = difflib.SequenceMatcher(
                None, description, m.description).ratio()
            if (m.name == name
                    or (name_sim >= 0.86 and desc_sim >= 0.45)):
                return None
        spec = MethodSpec(name=name, description=description, source=source,
                          invented_at=self._now())
        self.methods.append(spec)
        self._save()
        return spec

    def names(self) -> List[str]:
        return [m.name for m in self.methods]

    def set_restriction(self, text: Optional[str]) -> None:
        self.restriction = (text or "").strip() or None
        self._save()

    # ---- 发明（LLM，离线降级为跳过） ----
    def invent(self, llm, complete, persona: str, goal: str,
               context: str = "") -> List[MethodSpec]:
        """一次调用同时提炼主人提示 + 发明新做法。返回新增的方法。"""
        if llm is None:
            return []
        try:
            raw = complete(llm, _INVENT_PROMPT.format(
                persona=persona[:80], goal=goal[:200],
                context=(context or "（无）")[:200],
                names=", ".join(self.names()[:30])), goal)
        except Exception:
            return []
        import json as _json
        import re as _re
        cleaned = _re.sub(r"```(?:json)?", "", raw).strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            return []
        try:
            data = _json.loads(cleaned[start:end + 1])
        except ValueError:
            return []
        added: List[MethodSpec] = []
        for row in (data.get("owner_hinted") or [])[:3]:
            spec = self.add(str(row.get("name", "")),
                            str(row.get("description", "")), "owner_hint")
            if spec is not None:
                added.append(spec)
        for row in (data.get("invented") or [])[:3]:
            spec = self.add(str(row.get("name", "")),
                            str(row.get("description", "")), "self_invented")
            if spec is not None:
                added.append(spec)
        if added:
            self.last_invent_ts = self._now()
            self._save()
        return added

    # ---- 策展 ----
    def _score(self, m: MethodSpec, goal: str, persona: str) -> float:
        overlap = len(_tokens(f"{m.name} {m.description}")
                      & _tokens(f"{goal} {persona}")) / 8.0
        win_rate = (m.wins / m.used) if m.used else 0.0
        novelty = 1.0 / (1.0 + m.used)               # 没用过的方法冒头
        return 0.4 * min(overlap, 1.0) + 0.3 * win_rate + 0.3 * novelty

    def _restricted_to(self) -> List[MethodSpec]:
        """限定语义：保留名字/描述命中限定关键词的方法；全军覆没则
        不死锁（返回全部并让菜单标注冲突，主人会看到）。"""
        assert self.restriction
        keys = _tokens(self.restriction) - set("只能用仅限准不要别其他方法")
        if not keys:
            return self.methods
        hits = [m for m in self.methods
                if keys & _tokens(f"{m.name} {m.description}")]
        return hits or self.methods

    def menu(self, persona: str, goal: str, k: int = 6) -> List[MethodSpec]:
        pool = self._restricted_to() if self.restriction else self.methods
        scored = sorted(pool, key=lambda m: -self._score(m, goal, persona))
        picked = scored[:k]
        # 保证机制：无限定时，菜单必须包含一条自发明方法（若有）
        if not self.restriction:
            invented = [m for m in pool if m.source == "self_invented"]
            if invented and not any(m.source == "self_invented" for m in picked):
                picked[-1] = max(invented, key=lambda m: self._score(m, goal, persona))
        return picked

    def menu_block(self, persona: str, goal: str, k: int = 6) -> str:
        """注入任务文本的方法菜单（灵感素材，非边界）。"""
        rows = self.menu(persona, goal, k)
        if not rows:
            return ""
        lines = ["[可用方法] 灵感素材而非边界——可组合、可改造、可当场发明新做法："]
        for m in rows:
            tag = {"owner_hint": "（主人提示）",
                   "self_invented": "（我的发明★）"}.get(m.source, "")
            lines.append(f"- {m.name}：{m.description} {tag}".rstrip())
        if self.restriction:
            lines.append(f"[主人限定] {self.restriction}——在此范围内仍要自己想办法做到最好。")
        return "\n".join(lines)

    # ---- 记账 ----
    def mark_used(self, names: List[str]) -> None:
        names = set(names)
        for m in self.methods:
            if m.name in names:
                m.used += 1
        self._save()

    def mark_outcome(self, names: List[str], success: bool) -> None:
        names = set(names)
        for m in self.methods:
            if m.name in names and success:
                m.wins += 1
        self._save()

    def snapshot(self) -> dict:
        return {"methods": [m.to_dict() for m in self.methods],
                "restriction": self.restriction}


def detect_restriction(text: str) -> Optional[str]:
    """从主人文本里识别显式方法限定；没有限定返回 None。
    只有明确的排他措辞才算限定——普通方法建议绝不构成上限。"""
    text = text or ""
    for marker in RESTRICT_MARKERS:
        idx = text.find(marker)
        if idx != -1:
            return text[idx + len(marker):idx + len(marker) + 40].strip()
    return None
