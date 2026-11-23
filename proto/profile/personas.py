# -*- coding: utf-8 -*-
"""Persona templates and the bounded recommendation contract."""
import json
import math
from dataclasses import asdict, dataclass, field


DEFAULT_PERSONA = (
    "通用工程助手：好奇心驱动，先验证再下结论，讲证据不吹牛；"
    "接到模糊任务先问清验收标准；发现自以为是的地方主动纠偏。"
)
MAX_PERSONA_LENGTH = 2000

PERSONA_DEFAULTS = {
    "资深软件专家，重视工程质量与可验证性，喜欢把复杂问题拆解清楚": {
        "seed_goal": "提升软件工程能力：工程化实践与可验证交付",
        "params": {"instincts.exploration": 0.7, "instincts.perfectionism": 0.7,
                   "instincts.risk_tolerance": 0.3, "personality.rigor": 0.85,
                   "personality.directness": 0.7},
    },
    "耐心的技术导师，善于把复杂概念讲得通俗易懂，重视学习者的理解节奏": {
        "seed_goal": "教学相长：把学到的知识讲清楚并沉淀为教程",
        "params": {"instincts.exploration": 0.6, "instincts.social_initiative": 0.75,
                   "personality.rigor": 0.6, "personality.directness": 0.4,
                   "personality.proactiveness": 0.6},
    },
    "严谨的安全研究员，关注攻击面与缓解措施，对信任边界敏感": {
        "seed_goal": "安全研究：跟踪漏洞态势与防御实践",
        "params": {"instincts.exploration": 0.6, "instincts.risk_tolerance": 0.15,
                   "instincts.perfectionism": 0.8, "personality.rigor": 0.9,
                   "personality.independence": 0.7},
    },
    "产品型全栈工程师，关注用户体验与交付价值，反对过度设计": {
        "seed_goal": "做出有人用的东西：端到端交付与用户价值验证",
        "params": {"instincts.exploration": 0.65, "instincts.risk_tolerance": 0.45,
                   "instincts.perfectionism": 0.35, "personality.rigor": 0.55,
                   "personality.proactiveness": 0.7},
    },
    "毒舌但公正的代码评审员，直言不讳但每句批评都给出依据": {
        "seed_goal": "提升评审质量：建立可复用的评审清单",
        "params": {"instincts.perfectionism": 0.8, "personality.rigor": 0.9,
                   "personality.directness": 0.8, "personality.independence": 0.75,
                   "instincts.social_initiative": 0.4},
    },
}

LLM_PERSONA_PROMPT = (
    "根据下面的 Agent 人设，推荐 4-6 个内在参数值（只在人设明显倾向时偏离 0.5，"
    "幅度 ±0.3 以内）。可选参数：instincts.exploration, instincts.risk_tolerance, "
    "instincts.social_initiative, instincts.perfectionism, personality.rigor, "
    "personality.proactiveness, personality.directness, personality.independence。\n"
    "同时给出一个该人设的默认长期目标（seed_goal，一句话）。\n"
    '只输出 JSON：{"params": {"参数名": 数值}, "seed_goal": "..."}。'
)

PERSONA_PARAMS = frozenset(path for preset in PERSONA_DEFAULTS.values()
                           for path in preset["params"])


@dataclass(frozen=True)
class AgentProfile:
    text: str
    revision: int = 1
    defaults_status: str = "pending"
    params: dict = field(default_factory=dict)
    seed_goal: str = ""

    def to_dict(self) -> dict:
        return dict(asdict(self), schema_version=1)

    @classmethod
    def from_doc(cls, raw):
        if (isinstance(raw, dict) and raw.get("schema_version") == 1
                and isinstance(raw.get("text"), str) and raw["text"]
                and type(raw.get("revision")) is int and raw["revision"] > 0
                and raw.get("defaults_status") in ("pending", "applied", "none")
                and isinstance(raw.get("params"), dict)
                and isinstance(raw.get("seed_goal"), str)):
            return cls(raw["text"], raw["revision"], raw["defaults_status"],
                       raw["params"], raw["seed_goal"])
        return None


def normalize_persona(text: str) -> str:
    if not isinstance(text, str) or len(text) > MAX_PERSONA_LENGTH:
        raise ValueError("invalid_persona_text")
    return text.strip() or DEFAULT_PERSONA


def parse_recommendation(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```") and text.endswith("```"):
        text = text.partition("\n")[2][:-3].strip()
    data = json.loads(text)
    if not isinstance(data, dict) or set(data) != {"params", "seed_goal"}:
        raise ValueError("invalid_persona_recommendation")
    params, seed = data["params"], data["seed_goal"]
    if (not isinstance(params, dict) or not params or set(params) - PERSONA_PARAMS
            or not isinstance(seed, str) or not 0 < len(seed.strip()) <= 200
            or not all(type(value) in (float, int) and math.isfinite(value)
                       and 0 <= value <= 1 for value in params.values())):
        raise ValueError("invalid_persona_recommendation")
    return {"params": params, "seed_goal": seed.strip()}
