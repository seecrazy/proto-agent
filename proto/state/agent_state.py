# -*- coding: utf-8 -*-
"""L2 显式状态：AgentState（参数+情绪+调制日志）与 StateManager（单一写者，设计 R12）。"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from proto.kernel.audit import AuditEvent, AuditSpine
from proto.kernel.kernel import Kernel
from proto.state.schema import DEFAULT_SPECS, EMOTION_DECAY, EMOTION_SETPOINT
from proto.state.store import DocumentStore

SCHEMA_VERSION = 2
PARAM_CATEGORIES = ("instincts", "desires", "personality")


class StateValidationError(Exception):
    """状态文件载入校验失败（越界值/缺字段）。"""


@dataclass
class SetResult:
    value: float
    clamped: bool


@dataclass
class ModulationResult:
    applied: bool
    value: float
    clamped: bool = False
    reason: str = ""


def _round4(x: float) -> float:
    return round(float(x), 4)


class AgentState:
    """参数按 group.name 寻址，如 "instincts.exploration"。"""

    def __init__(self, specs: Dict[str, Dict[str, Dict]] = None):
        source = specs if specs is not None else DEFAULT_SPECS
        self.instincts: Dict[str, Dict] = copy.deepcopy(source["instincts"])
        self.desires: Dict[str, Dict] = copy.deepcopy(source["desires"])
        self.personality: Dict[str, Dict] = copy.deepcopy(source["personality"])
        self.emotion: Dict[str, float] = dict(EMOTION_SETPOINT)
        self.owner_priority: str = ""
        self.modulation_log: List[dict] = []
        for group in (self.instincts, self.desires, self.personality):
            for spec in group.values():
                spec.setdefault("owner_locked", False)

    # ---- 寻址 ----
    def _resolve(self, path: str) -> Tuple[Dict[str, Dict], str]:
        group_name, _, name = path.partition(".")
        groups = {"instincts": self.instincts, "desires": self.desires, "personality": self.personality}
        if group_name not in groups or name not in groups[group_name]:
            raise KeyError(f"未知参数路径: {path}")
        return groups[group_name], name

    def get(self, path: str) -> float:
        group, name = self._resolve(path)
        return group[name]["value"]

    def spec(self, path: str) -> Dict:
        group, name = self._resolve(path)
        return group[name]

    def bounds(self, path: str) -> Tuple[float, float]:
        return tuple(self.spec(path)["bounds"])

    def set_within_bounds(self, path: str, value: float) -> SetResult:
        """有界写入（内核 clamp 语义）。经审计的调制请走 StateManager。"""
        group, name = self._resolve(path)
        lo, hi = group[name]["bounds"]
        clamped = not (lo <= value <= hi)
        final = _round4(max(lo, min(hi, value)))
        group[name]["value"] = final
        return SetResult(final, clamped)

    def lock(self, path: str) -> None:
        self.spec(path)["owner_locked"] = True

    def unlock(self, path: str) -> None:
        self.spec(path)["owner_locked"] = False

    def all_params(self) -> List[str]:
        paths = []
        for cat in PARAM_CATEGORIES:
            for name in getattr(self, cat):
                paths.append(f"{cat}.{name}")
        return paths

    # ---- 衰减：向设定点回归；personality 速率 ×(1-stability)（定型棘轮）----
    def tick(self, n: int = 1) -> None:
        for group in (self.instincts, self.desires, self.personality):
            for spec in group.values():
                rate = spec["decay"] * (1.0 - spec.get("stability", 0.0)) * n
                spec["value"] = _round4(spec["value"] + (spec["setpoint"] - spec["value"]) * rate)
        for key, val in self.emotion.items():
            rate = EMOTION_DECAY[key] * n
            self.emotion[key] = _round4(val + (EMOTION_SETPOINT[key] - val) * rate)

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        """返回深拷贝——修改返回值不影响活状态（M8）。"""
        return {
            "schema_version": SCHEMA_VERSION,
            "instincts": copy.deepcopy(self.instincts),
            "desires": copy.deepcopy(self.desires),
            "personality": copy.deepcopy(self.personality),
            "emotion": dict(self.emotion),
            "owner_priority": self.owner_priority,
            "modulation_log": copy.deepcopy(self.modulation_log),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "AgentState":
        """载入校验：以内核权威界（DEFAULT_SPECS / 情绪 BOUNDS）为准，
        文件自带的 bounds 不可自证（R12 场景：主人手改状态文件）。"""
        from proto.state.emotion import BOUNDS as EMOTION_BOUNDS

        def _reject(msg: str):
            raise StateValidationError(msg)

        for cat in PARAM_CATEGORIES:
            if not isinstance(raw, dict) or cat not in raw:
                _reject(f"缺少字段: {cat}")
        state = cls()
        for cat in PARAM_CATEGORIES:
            for name, spec in raw[cat].items():
                if name not in getattr(state, cat):
                    _reject(f"未知参数: {cat}.{name}")
                if not isinstance(spec, dict):
                    _reject(f"{cat}.{name} 规格不是对象")
                for field in ("value", "setpoint", "bounds"):
                    if field not in spec:
                        _reject(f"{cat}.{name} 缺少字段: {field}")
                authoritative = state.spec(f"{cat}.{name}")
                if list(spec["bounds"]) != list(authoritative["bounds"]):
                    _reject(f"{cat}.{name} bounds {spec['bounds']} 与内核权威界 "
                            f"{authoritative['bounds']} 不一致")
                lo, hi = authoritative["bounds"]
                value = spec["value"]
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    _reject(f"{cat}.{name} 值类型非法: {value!r}")
                if not (lo <= value <= hi):
                    _reject(f"{cat}.{name} 值 {value} 越界 [{lo}, {hi}]")
        emotion = raw.get("emotion", {})
        if not isinstance(emotion, dict):
            _reject("emotion 不是对象")
        for key, (lo, hi) in EMOTION_BOUNDS.items():
            if key in emotion:
                v = emotion[key]
                if not isinstance(v, (int, float)) or isinstance(v, bool) or not (lo <= v <= hi):
                    _reject(f"emotion.{key} 值 {v!r} 越界 [{lo}, {hi}]")
        state.instincts = copy.deepcopy(raw["instincts"])
        state.desires = copy.deepcopy(raw["desires"])
        state.personality = copy.deepcopy(raw["personality"])
        state.emotion = dict(raw.get("emotion", EMOTION_SETPOINT))
        state.owner_priority = raw.get("owner_priority", "")
        state.modulation_log = list(raw.get("modulation_log", []))
        return state


class StateManager:
    """L2 唯一写者。所有参数变更必须经此处（边界 + 锁检查 + 审计 + 落盘）。"""

    def __init__(self, store: DocumentStore, state: AgentState, kernel: Kernel,
                 audit: AuditSpine, agent_id: str = "proto-001"):
        self._store = store
        self.state = state
        self._kernel = kernel
        self._audit = audit
        self._agent_id = agent_id

    def apply_modulation(self, param: str, delta: float, source: str,
                         accumulated: float = None) -> ModulationResult:
        spec = self.state.spec(param)
        if spec.get("owner_locked"):
            self._audit.append(AuditEvent.VETO, {
                "param": param, "reason": "owner_locked", "source": source, "delta": delta})
            return ModulationResult(False, spec["value"], reason="owner_locked")
        current = spec["value"]
        target = current + delta
        result = self.state.set_within_bounds(param, target)
        audit_seq = self._audit.append(AuditEvent.MODULATION, {
            "param": param, "delta": delta, "source": source,
            "pre": _round4(current), "post": result.value, "clamped": result.clamped,
        })
        entry = {
            "param": param, "delta": _round4(delta), "source": source,
            "clamped": result.clamped, "audit_seq": audit_seq,
            "pre": _round4(current), "post": result.value,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if accumulated is not None:
            entry["accumulated"] = _round4(accumulated)
        self.state.modulation_log.append(entry)
        self.save()
        return ModulationResult(True, result.value, result.clamped)

    def owner_override(self, path: str, value: float) -> SetResult:
        if self.state.spec(path).get("owner_locked"):
            self._audit.append(AuditEvent.VETO, {
                "param": path, "reason": "owner_locked", "source": "owner_override"})
            raise PermissionError(f"{path} 已被主人锁定，override 亦不可写（先解锁）")
        result = self.state.set_within_bounds(path, value)
        self._audit.append(AuditEvent.OWNER_OVERRIDE, {"param": path, "post": result.value, "clamped": result.clamped})
        self.save()
        return result

    def save(self) -> None:
        self._store.put("state", self._agent_id, self.state.to_dict())

    def load_raw(self) -> Optional[dict]:
        return self._store.get("state", self._agent_id)
