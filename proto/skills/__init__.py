"""Trusted, packaged engineering playbooks; they do not grant tool access."""
from __future__ import annotations

import hashlib
from functools import lru_cache
from importlib.resources import files


_NAMES = ("design-development-verification", "swift-ios-build", "local-verification")
_MAX_BYTES = 2500


@lru_cache(maxsize=len(_NAMES))
def _load(name):
    if name not in _NAMES:
        raise ValueError("unknown_skill")
    with files(__package__).joinpath(name).joinpath("SKILL.md").open("rb") as stream:
        payload = stream.read(_MAX_BYTES + 1)
    if len(payload) > _MAX_BYTES:
        raise ValueError("skill_too_large")
    opening, metadata, body = payload.decode("utf-8").split("---\n", 2)
    header = dict(line.split(": ", 1) for line in metadata.splitlines())
    if opening or header.get("name") != name or not header.get("description") or not body.strip():
        raise ValueError("invalid_skill")
    return header["description"], body.strip(), hashlib.sha256(payload).hexdigest()


def catalog():
    return "；".join(f"{name}：{_load(name)[0]}" for name in _NAMES)


def read_skill(name):
    description, instructions, digest = _load(name)
    return {"name": name, "description": description, "instructions": instructions, "sha256": digest}
