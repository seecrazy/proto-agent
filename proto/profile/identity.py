# -*- coding: utf-8 -*-
"""M39: 内在生活内核增强——OCEAN 数值人格 + 统一张力信号。

设计对照（用户设计稿 → PROTO 落点）：
- 人格层数值化：OCEAN 五因子 + 价值观 + 说话风格，跨会话持久；
  不硬编码进提示词规则，而以"软约束描述"注入系统提示词。
- 人格→驱力调制：内向者社交阈值更高、开放者好奇涨得更快——
  OCEAN 直接调制驱力的累积速率与触发阈值。
- 人格有界漂移：重大经历（反复受挫/成功/被夸/被拒）以 drift_rate
  微调因子，双重护栏：单因子夹在 [0.05, 0.95]、总漂移距种子 ≤0.15，
  防人格崩塌；每日深度回顾向种子回拉（离线反思的人格修正）。
- 统一张力：tension = Σ w_i·drive_i·urgency_i + arousal 项，
  取代逐维阈值散射；人格决定权重（尽责者目标紧迫权重高）。
- 说话风格：verbosity/formality/humor 数值 → 软约束文本。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

OCEAN_FACTORS = ("openness", "conscientiousness", "extraversion",
                 "agreeableness", "neuroticism")

DEFAULT_OCEAN = {
    "openness": 0.8,          # 求知默认偏高：这是成长型学徒的底色
    "conscientiousness": 0.6,
    "extraversion": 0.35,
    "agreeableness": 0.55,
    "neuroticism": 0.35,
}

MAX_TOTAL_DRIFT = 0.15        # 距种子的总漂移上限（防崩塌）
FACTOR_BOUNDS = (0.05, 0.95)

# 重大经历 → 有向漂移（幅度=drift_rate 的倍数）
_DRIFT_RULES = {
    # 事件: {因子: 有向幅度倍数}——幅度乘 drift_rate 后应用
    "praise":        {"agreeableness": +0.6, "neuroticism": -0.4},
    "rejection":     {"neuroticism": +0.8, "agreeableness": -0.3},
    "task_success":  {"conscientiousness": +0.4, "openness": +0.2},
    "task_failure":  {"neuroticism": +0.5, "conscientiousness": +0.2},
    "novel_insight": {"openness": +0.7},
}

DEFAULT_VALUES = ["求真", "不敷衍"]
DEFAULT_SPEECH = {"verbosity": 0.4, "formality": 0.5, "humor": 0.2}


def _clamp(v: float) -> float:
    return max(FACTOR_BOUNDS[0], min(FACTOR_BOUNDS[1], v))


@dataclass
class Identity:
    ocean: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_OCEAN))
    values: list = field(default_factory=lambda: list(DEFAULT_VALUES))
    speech_style: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_SPEECH))
    drift_rate: float = 0.004
    seed: Dict[str, float] = field(default_factory=lambda: dict(DEFAULT_OCEAN))

    # ---- 持久化 ----
    def to_dict(self) -> dict:
        return {"ocean": self.ocean, "values": self.values,
                "speech_style": self.speech_style, "drift_rate": self.drift_rate,
                "seed": self.seed}

    @classmethod
    def from_dict(cls, raw: dict) -> "Identity":
        return cls(ocean={k: float(v) for k, v in (raw.get("ocean") or DEFAULT_OCEAN).items()},
                   values=list(raw.get("values") or DEFAULT_VALUES),
                   speech_style={k: float(v) for k, v in
                                 (raw.get("speech_style") or DEFAULT_SPEECH).items()},
                   drift_rate=float(raw.get("drift_rate", 0.004)),
                   seed={k: float(v) for k, v in (raw.get("seed") or DEFAULT_OCEAN).items()})

    # ---- 漂移（双重护栏）----
    def apply_drift(self, event: str) -> Dict[str, float]:
        """重大经历驱动的有界漂移；返回实际变化（可观测）。"""
        rule = _DRIFT_RULES.get(event)
        if not rule:
            return {}
        changed: Dict[str, float] = {}
        for factor, mult in rule.items():
            if factor not in self.ocean:
                continue
            target = _clamp(self.ocean[factor] + self.drift_rate * mult)
            # 总漂移护栏：距种子累计漂移不超过 MAX_TOTAL_DRIFT
            drift_budget = MAX_TOTAL_DRIFT - abs(target - self.seed[factor])
            if drift_budget < 0:
                direction = 1 if target > self.seed[factor] else -1
                target = self.seed[factor] + direction * MAX_TOTAL_DRIFT
                target = _clamp(target)
            if abs(target - self.ocean[factor]) > 1e-9:
                changed[factor] = round(target - self.ocean[factor], 5)
                self.ocean[factor] = target
        return changed

    def pull_back(self, fraction: float = 0.1) -> None:
        """深度回顾的人格修正：向种子回拉一小步（防长期单向社会化）。"""
        for factor, seed_v in self.seed.items():
            current = self.ocean.get(factor, seed_v)
            self.ocean[factor] = _clamp(
                current + (seed_v - current) * fraction)

    # ---- 人格 → 驱力调制 ----
    def drive_modulators(self) -> Dict[str, Dict[str, float]]:
        """驱力的人格调制：rate_mult 累积速率倍数 / threshold_delta 阈值增量。"""
        o = self.ocean
        return {
            "curiosity_gap": {
                "rate_mult": 0.6 + 0.8 * o["openness"],          # 开放者好奇快涨
                "threshold_delta": 0.0,
            },
            "social_connection_gap": {
                "rate_mult": 0.5 + 1.0 * o["extraversion"],
                # 内向者社交阈值更高：不轻易打扰，但憋久了照样越阈
                "threshold_delta": (0.5 - o["extraversion"]) * 0.2,
            },
            "goal_urgency": {
                "rate_mult": 0.7 + 0.6 * o["conscientiousness"],
                "threshold_delta": 0.0,
            },
            "recognition_need": {
                "rate_mult": 0.6 + 0.8 * o["neuroticism"],
                "threshold_delta": 0.0,
            },
        }

    # ---- 人格 → 张力权重 ----
    def tension_weights(self) -> Dict[str, float]:
        o = self.ocean
        return {
            "curiosity_gap": 0.6 + 0.6 * o["openness"],
            "social_connection_gap": 0.4 + 0.8 * o["extraversion"],
            "goal_urgency": 0.6 + 0.8 * o["conscientiousness"],
            "recognition_need": 0.4 + 0.5 * o["neuroticism"],
        }

    # ---- 软约束提示块 ----
    def prompt_block(self) -> str:
        o = self.ocean
        traits = []
        traits.append("开放爱钻研" if o["openness"] >= 0.6 else "务实聚焦")
        traits.append("外向健谈" if o["extraversion"] >= 0.5 else "偏内向安静")
        if o["neuroticism"] >= 0.5:
            traits.append("易焦虑")
        if o["conscientiousness"] >= 0.6:
            traits.append("做事有始有终")
        s = self.speech_style
        style = []
        style.append("说话简洁" if s.get("verbosity", 0.4) < 0.5 else "表达充分")
        style.append("正式" if s.get("formality", 0.5) >= 0.5 else "随和口语")
        if s.get("humor", 0.2) >= 0.5:
            style.append("偶尔来点幽默")
        return (f"[我的性格底色] {('、'.join(traits))}；"
                f"价值观：{'、'.join(self.values)}；"
                f"风格：{'、'.join(style)}。这是自然倾向不是人设台词。")


def load_identity(store) -> Identity:
    raw = (store.get("identity", "main") or {}) if store is not None else {}
    return Identity.from_dict(raw) if raw else Identity()


def save_identity(store, identity: Identity) -> None:
    if store is not None:
        store.put("identity", "main", identity.to_dict())
