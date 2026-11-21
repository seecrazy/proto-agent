# -*- coding: utf-8 -*-
"""持久模型目录与请求边界的主/快模型热切换。"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from proto.llm.client import (AnthropicCompatClient, LLMClient, LLMError,
                              OpenAICompatClient)


class ModelConfigError(ValueError):
    pass


class ModelRevisionConflict(ModelConfigError):
    pass


def _text(value, maximum=512, empty=False):
    if not isinstance(value, str) or len(value) > maximum:
        raise ModelConfigError("invalid_model_field")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ModelConfigError("invalid_model_field")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ModelConfigError("invalid_model_field") from None
    value = value.strip()
    if not value and not empty:
        raise ModelConfigError("missing_model_field")
    return value


def _profile(value):
    fields = {"id", "name", "api_style", "base_url", "model", "api_key"}
    if not isinstance(value, dict) or set(value) != fields:
        raise ModelConfigError("invalid_model_profile")
    result = {key: _text(value[key], 4096 if key == "api_key" else 512,
                         empty=key == "api_key") for key in fields}
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", result["id"]):
        raise ModelConfigError("invalid_model_id")
    if result["api_style"] not in ("openai", "anthropic"):
        raise ModelConfigError("unsupported_model_protocol")
    try:
        parsed = urlsplit(result["base_url"])
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.port == 0
                or any(character.isspace() for character in result["base_url"])):
            raise ValueError()
    except ValueError:
        raise ModelConfigError("invalid_model_url") from None
    result["base_url"] = result["base_url"].rstrip("/")
    return result


def _document(value):
    if not isinstance(value, dict) or set(value) != {
            "schema_version", "revision", "profiles", "selection"}:
        raise ModelConfigError("invalid_model_catalog")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ModelConfigError("unsupported_model_catalog_version")
    if type(value["revision"]) is not int or value["revision"] < 0:
        raise ModelConfigError("invalid_model_revision")
    if not isinstance(value["profiles"], list) or len(value["profiles"]) > 64:
        raise ModelConfigError("invalid_model_profiles")
    profiles = [_profile(profile) for profile in value["profiles"]]
    identities = {profile["id"] for profile in profiles}
    if len(identities) != len(profiles):
        raise ModelConfigError("duplicate_model_id")
    selection = value["selection"]
    if not isinstance(selection, dict) or set(selection) != {"main", "fast"}:
        raise ModelConfigError("invalid_model_selection")
    for identity in selection.values():
        if identity is not None and (not isinstance(identity, str)
                                     or identity not in identities):
            raise ModelConfigError("unknown_model")
    if selection["main"] is None and selection["fast"] is not None:
        raise ModelConfigError("missing_main_model")
    return {"schema_version": 1, "revision": value["revision"],
            "profiles": profiles, "selection": dict(selection)}


def _from_client(client, identity, name):
    if not isinstance(client, (OpenAICompatClient, AnthropicCompatClient)):
        return None
    return _profile({"id": identity, "name": name, "base_url": client._base,
                     "api_key": client._key, "model": client._model,
                     "api_style": ("anthropic" if isinstance(client, AnthropicCompatClient)
                                   else "openai")})


class _PinnedClient(LLMClient):
    def __init__(self, client, binding_key):
        self._client = client
        self.binding_key = binding_key

    def complete(self, system, user):
        self.last_usage = None
        result = self._client.complete(system, user)
        self.last_usage = copy.deepcopy(getattr(self._client, "last_usage", None))
        return result


class ModelRoleClient(LLMClient):
    def __init__(self, catalog, role):
        self._catalog = catalog
        self._role = role
        self._local = threading.local()

    @property
    def last_usage(self):
        return getattr(self._local, "usage", None)

    def pin(self):
        return self._catalog.pin(self._role)

    def describe(self):
        return self._catalog.describe(self._role)

    def complete(self, system, user):
        self._local.usage = None
        client = self.pin()
        result = client.complete(system, user)
        self._local.usage = client.last_usage
        return result


class ModelCatalog:
    def __init__(self, root: Path, main=None, fast=None):
        self.path = Path(root) / "llm_models.json"
        self._lock = threading.RLock()
        self._fallback = {"main": main, "fast": fast if fast is not None else main}
        self._roles = {role: ModelRoleClient(self, role) for role in ("main", "fast")}
        self._state = {"schema_version": 1, "revision": 0, "profiles": [],
                       "selection": {"main": None, "fast": None}}
        if self.path.exists() or self.path.is_symlink():
            self._state = self._read()
            self.path.chmod(0o600)
            self._fallback = {"main": None, "fast": None}
        else:
            primary = _from_client(main, "env-main", "启动主模型")
            secondary = _from_client(fast, "env-fast", "启动快模型")
            if primary:
                self._state["profiles"].append(primary)
                self._state["selection"]["main"] = primary["id"]
                if secondary and fast is not main:
                    self._state["profiles"].append(secondary)
                    self._state["selection"]["fast"] = secondary["id"]

    def _read(self):
        try:
            if self.path.is_symlink():
                raise ModelConfigError("model_catalog_symlink")
            with self.path.open("rb") as source:
                data = source.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ModelConfigError("model_catalog_too_large")
            return _document(json.loads(data.decode("utf-8")))
        except (OSError, ValueError, UnicodeError, RecursionError) as exc:
            if isinstance(exc, ModelConfigError):
                raise
            raise ModelConfigError("model_catalog_read_failed") from None

    def _write(self, state):
        temporary = None
        try:
            data = json.dumps(state, ensure_ascii=False, indent=2).encode("utf-8")
            if len(data) > 1024 * 1024:
                raise ModelConfigError("model_catalog_too_large")
            if self.path.is_symlink():
                raise ModelConfigError("model_catalog_symlink")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".llm-models-", dir=self.path.parent)
            with os.fdopen(descriptor, "wb") as target:
                os.fchmod(target.fileno(), 0o600)
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise ModelConfigError("model_catalog_write_failed") from None
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def _check_revision(self, expected):
        if type(expected) is not int or expected < 0:
            raise ModelConfigError("invalid_model_revision")
        if expected != self._state["revision"]:
            raise ModelRevisionConflict("model_revision_conflict")

    def _publish(self, candidate):
        candidate = _document(candidate)
        candidate["revision"] = self._state["revision"] + 1
        self._write(candidate)
        self._state = candidate
        if candidate["selection"]["main"] is not None:
            self._fallback = {"main": None, "fast": None}
        return self.view()

    def view(self):
        with self._lock:
            state = copy.deepcopy(self._state)
            for profile in state["profiles"]:
                profile["has_api_key"] = bool(profile.pop("api_key"))
            state["external_client"] = (state["selection"]["main"] is None
                                        and self._fallback["main"] is not None)
            return state

    def save_profile(self, profile, expected_revision):
        with self._lock:
            self._check_revision(expected_revision)
            fields = {"id", "name", "api_style", "base_url", "model", "api_key", "clear_api_key"}
            if not isinstance(profile, dict) or set(profile) - fields:
                raise ModelConfigError("invalid_model_profile")
            identity = profile["id"] if "id" in profile else uuid.uuid4().hex[:12]
            if not isinstance(identity, str):
                raise ModelConfigError("invalid_model_id")
            current = next((row for row in self._state["profiles"] if row["id"] == identity), {})
            candidate = copy.deepcopy(self._state)
            merged = dict(current)
            merged.update({key: value for key, value in profile.items()
                           if key not in ("api_key", "clear_api_key")})
            merged["id"] = identity
            clear = profile.get("clear_api_key", False)
            if type(clear) is not bool:
                raise ModelConfigError("invalid_api_key_action")
            key = profile.get("api_key", "")
            key = _text(key, 4096, empty=True)
            if clear and key:
                raise ModelConfigError("conflicting_api_key_action")
            merged["api_key"] = "" if clear else key or current.get("api_key", "")
            merged = _profile(merged)
            candidate["profiles"] = [row for row in candidate["profiles"] if row["id"] != identity]
            candidate["profiles"].append(merged)
            result = self._publish(candidate)
            result["saved_id"] = identity
            return result

    def select(self, main, fast, expected_revision):
        with self._lock:
            self._check_revision(expected_revision)
            if not isinstance(main, str) or not main:
                raise ModelConfigError("missing_main_model")
            candidate = copy.deepcopy(self._state)
            candidate["selection"] = {"main": main, "fast": fast}
            return self._publish(candidate)

    def reload(self, expected_revision):
        with self._lock:
            self._check_revision(expected_revision)
            return self._publish(self._read())

    def client(self, role):
        with self._lock:
            active = self._state["selection"]["main"] is not None
            return self._roles[role] if active else self._fallback[role]

    @property
    def uses_injected_clients(self):
        with self._lock:
            return (self._state["selection"]["main"] is None
                    and self._fallback["main"] is not None)

    def pin(self, role):
        with self._lock:
            selection = self._state["selection"]
            identity = selection[role] or selection["main"]
            if identity is None:
                client = self._fallback[role]
                if client is None:
                    raise LLMError("model_not_configured")
                return _PinnedClient(client, ("injected", id(client)))
            profile = next(row for row in self._state["profiles"] if row["id"] == identity)
            cls = AnthropicCompatClient if profile["api_style"] == "anthropic" else OpenAICompatClient
            client = cls(base_url=profile["base_url"], api_key=profile["api_key"], model=profile["model"])
            fingerprint = hashlib.sha256(json.dumps(
                {key: profile[key] for key in ("api_style", "base_url", "model", "api_key")},
                sort_keys=True).encode("utf-8")).hexdigest()
            return _PinnedClient(client, (identity, fingerprint))

    def describe(self, role):
        with self._lock:
            selection = self._state["selection"]
            identity = selection[role] or selection["main"]
            if identity:
                profile = next(row for row in self._state["profiles"] if row["id"] == identity)
                return f"{profile['model']} @ {profile['base_url']} ({profile['api_style']})"
            client = self._fallback[role]
            return client.describe() if hasattr(client, "describe") else "启动注入客户端"
