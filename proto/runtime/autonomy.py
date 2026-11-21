"""Owner-controlled policy, independent of persona and budget source."""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import asdict, dataclass
from functools import wraps


MODES = ("manual", "suggest", "auto")
SCHEMA_VERSION = 1
autonomous_origin = ContextVar("autonomous_origin", default=None)


class AutonomyConflict(ValueError):
    pass


def validate_mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError("invalid_autonomy_mode")
    return mode


def autonomous_entry(method):
    @wraps(method)
    def invoke(agent, *args, **kwargs):
        token = autonomous_origin.set(agent)
        try:
            return method(agent, *args, **kwargs)
        finally:
            autonomous_origin.reset(token)

    return invoke


def load_policy(store, initial=None):
    try:
        raw = store.get("config", "autonomy")
    except ValueError:
        return AutonomyPolicy(revision=0), False
    if raw is None:
        return initial or AutonomyPolicy(enabled=True, mode="auto"), True
    return AutonomyPolicy.from_doc(raw), False


@dataclass(frozen=True)
class AutonomyPolicy:
    enabled: bool = False
    mode: str = "manual"
    revision: int = 1

    @classmethod
    def from_doc(cls, raw):
        if (not isinstance(raw, dict) or type(raw.get("schema_version")) is not int
                or raw["schema_version"] != SCHEMA_VERSION
                or type(raw.get("enabled")) is not bool or raw.get("mode") not in MODES
                or type(raw.get("revision")) is not int or raw["revision"] < 1):
            return cls(revision=0)
        return cls(raw["enabled"], raw["mode"], raw["revision"])

    def to_dict(self) -> dict:
        return dict(asdict(self), schema_version=SCHEMA_VERSION)

    def blocker(self, *, paused=False, muted=False, suggestions=False) -> str:
        if paused:
            return "fleet_paused"
        if muted:
            return "muted"
        if not self.enabled:
            return "not_enabled"
        if self.mode == "manual":
            return "manual_only"
        if self.mode == "suggest" and not suggestions:
            return "suggestions_only"
        return ""
