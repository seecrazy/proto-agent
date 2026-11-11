# -*- coding: utf-8 -*-
"""L2 默认参数体系（设计 §6 / §11.1）。

每参数 = 值 + 设定点 + 界 + 衰减率 + 行为代理（proxy）。
desires 额外带触发阈值；personality 额外带 stability（定型棘轮，变化速率 ×(1-stability)）。
"""
from __future__ import annotations

from typing import Dict

# proxy 名称即评估 harness 的指标键（设计 §6.2 表）
DEFAULT_SPECS: Dict[str, Dict[str, Dict]] = {
    "instincts": {
        "exploration": {"value": 0.6, "setpoint": 0.5, "bounds": [0.1, 0.9], "decay": 0.01, "proxy": "tool_diversity"},
        "risk_tolerance": {"value": 0.4, "setpoint": 0.4, "bounds": [0.1, 0.7], "decay": 0.01, "proxy": "risky_choice_ratio"},
        "social_initiative": {"value": 0.5, "setpoint": 0.5, "bounds": [0.1, 0.9], "decay": 0.01, "proxy": "proactive_proposals_per_day"},
        "perfectionism": {"value": 0.5, "setpoint": 0.5, "bounds": [0.1, 0.9], "decay": 0.01, "proxy": "verification_rounds"},
    },
    "desires": {
        "curiosity_gap": {"value": 0.3, "setpoint": 0.5, "bounds": [0.0, 1.0], "threshold": 0.6, "threshold_bounds": [0.3, 0.95], "decay": 0.05, "proxy": "self_retrieval_rate"},
        "goal_urgency": {"value": 0.7, "setpoint": 0.3, "bounds": [0.0, 1.0], "threshold": 0.5, "threshold_bounds": [0.3, 0.95], "decay": 0.08, "proxy": "goal_stack_velocity"},
        "social_connection_gap": {"value": 0.4, "setpoint": 0.5, "bounds": [0.0, 1.0], "threshold": 0.6, "threshold_bounds": [0.3, 0.95], "decay": 0.05, "proxy": "owner_interaction_rate"},
        "recognition_need": {"value": 0.4, "setpoint": 0.5, "bounds": [0.0, 1.0], "threshold": 0.6, "threshold_bounds": [0.3, 0.95], "decay": 0.05, "proxy": "feedback_request_rate"},
    },
    "personality": {
        "rigor": {"value": 0.7, "setpoint": 0.7, "bounds": [0.2, 0.9], "decay": 0.01, "stability": 0.95, "proxy": "verification_ratio"},
        "proactiveness": {"value": 0.5, "setpoint": 0.5, "bounds": [0.2, 0.8], "decay": 0.01, "stability": 0.95, "proxy": "initiative_ratio"},
        "directness": {"value": 0.5, "setpoint": 0.5, "bounds": [0.2, 0.8], "decay": 0.01, "stability": 0.95, "proxy": "hedging_ratio"},
        "independence": {"value": 0.5, "setpoint": 0.5, "bounds": [0.1, 0.8], "decay": 0.01, "stability": 0.95, "proxy": "escalation_ratio"},
    },
}

EMOTION_SETPOINT = {"pleasure": 0.0, "arousal": 0.4, "dominance": 0.5}
EMOTION_DECAY = {"pleasure": 0.10, "arousal": 0.15, "dominance": 0.10}
