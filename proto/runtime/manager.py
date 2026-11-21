# -*- coding: utf-8 -*-
"""M21: 多自主 Agent 管理——创建/管理/观测一支 Agent 舰队。

一个主人（owner），多个 Agent：各有独立身份（锚定/人设/记忆/目标树/预算），
共享 LLM 配置。注册表在 root/registry.json；数据在各 agents/<id>/；
归档不删除，托管目录移入 archive/，收编目录原地保留。心跳统一驱动全舰队（暂停者跳过）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager
from copy import copy
from pathlib import Path
from typing import Dict, List, Optional

from proto.runtime.agent import ProtoAgent, pools_config
from proto.runtime.autonomy import AutonomyPolicy, load_policy, validate_mode
from proto.runtime.coordination import CoordinationError
from proto.storage.mongo_store import create_store
from proto.runtime.collab import Blackboard
from proto.runtime.personas import AgentProfile, DEFAULT_PERSONA, PERSONA_DEFAULTS, normalize_persona
from proto.runtime.runs import RunCoordinator

REGISTRY = "registry.json"


class AgentManager:
    def __init__(self, root: Path, llm=None, fast_llm=None, webtools=None,
                 blackboard=None, models=None):
        self.root = Path(root)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        (self.root / "agents").mkdir(parents=True, exist_ok=True)
        (self.root / "archive").mkdir(parents=True, exist_ok=True)
        self._llm = llm
        self._fast = fast_llm if fast_llm is not None else llm
        self.models = models
        self._webtools = webtools
        self.blackboard = blackboard or Blackboard(create_store(self.root))
        import threading as _th
        self._ops_locks = {}            # M50: per-agent 单写者锁
        self._ops_locks_guard = _th.Lock()
        self._cache: Dict[str, ProtoAgent] = {}
        self._active_accesses: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._registry_path = self.root / REGISTRY
        self.runs = RunCoordinator(self)

    def enable_model_updates(self):
        from proto.llm.catalog import ModelCatalog
        with self._lock:
            if self.models is None:
                root = self.root.parent if self.root.name == ".fleet" else self.root
                self.models = ModelCatalog(root, self._llm, self._fast)
                for agent in self._cache.values():
                    agent.bind_model_catalog(self.models)
            return self.models

    # ---- 注册表 ----
    @property
    def initialized(self) -> bool:
        return self._registry_path.is_file()

    def _load_registry(self) -> dict:
        if self._registry_path.exists():
            try:
                reg = json.loads(self._registry_path.read_text(encoding="utf-8"))
            except ValueError:
                reg = {"agents": [], "current": None}
        else:
            reg = {"agents": [], "current": None}
        # 自动去重历史脏数据：同一 external_dir/目录 只保留最新一条
        seen = {}
        for a in reg["agents"]:
            key = a.get("external_dir") or str(self.root / "agents" / a["id"])
            if key not in seen:
                seen[key] = a
            else:
                seen[key] = a                 # 后写的覆盖（保留最新 created_at 语义）
        reg["agents"] = list(seen.values())
        if reg.get("current") not in [a["id"] for a in reg["agents"]]:
            reg["current"] = reg["agents"][0]["id"] if reg["agents"] else None
        return reg

    def _save_registry(self, reg: dict) -> None:
        tmp = self._registry_path.with_suffix(f".json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.chmod(0o600)
        os.replace(tmp, self._registry_path)
        try:
            self._registry_path.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _allocate_id(reg: dict, preferred: str = "") -> str:
        used = {row["id"] for collection in ("agents", "archived") for row in reg.get(collection, [])}
        if re.fullmatch(r"[A-Za-z0-9_-]+", preferred) and preferred not in used:
            return preferred
        while True:
            candidate = uuid.uuid4().hex[:10]
            if candidate not in used:
                return candidate

    # ---- 生命周期 ----
    def preview(self, name: str, persona: str = "", priority: str = "", mode: str = "manual") -> dict:
        return self.preview_at(self.root, name, persona, priority, mode)

    @staticmethod
    def preview_at(fleet_root: Path, name: str, persona: str = "", priority: str = "",
                   mode: str = "manual") -> dict:
        if not isinstance(name, str) or not isinstance(priority, str) or len(priority) > 2000:
            raise ValueError("invalid_agent_fields")
        text = normalize_persona(persona)
        preset = PERSONA_DEFAULTS.get(text, {})
        root = os.environ.get("PROTO_PROJECTS_ROOT")
        project = Path(root) / "<agent-id>" if root else fleet_root / "agents" / "<agent-id>" / "workspace"
        return {
            "name": name.strip()[:40] or "Agent", "persona": text, "priority": priority.strip(),
            "profile": {"params": dict(preset.get("params", {})), "seed_goal": preset.get("seed_goal", ""),
                        "defaults_status": "applied" if preset else "none" if text == DEFAULT_PERSONA else "pending"},
            "autonomy": AutonomyPolicy(mode=validate_mode(mode)).to_dict(),
            "scope": {"projects_root": str(project), "budget_caps": pools_config(),
                      "tools": ["工作区读写与受限验证", "白名单资料阅读", "舰队黑板与状态"],
                      "approval": "现有 L1 审批、超时风险限制和工作区隔离继续生效；人设不授予权限"},
        }

    def create(self, name: str, persona: str = "", priority: str = "", mode: str = "manual") -> dict:
        draft = self.preview(name, persona, priority, mode)
        with self._lock:
            reg = self._load_registry()
            info = {"id": self._allocate_id(reg), "name": draft["name"],
                    "created_at": time.time(), "paused": False}
            agent = self._build(info, draft["persona"], AutonomyPolicy(mode=mode))
            if draft["priority"]:
                agent.set_priority_text(draft["priority"])
            reg["agents"].append(info)
            reg["current"] = reg["current"] or info["id"]
            self._save_registry(reg)
            self._cache[info["id"]] = agent
            return dict(info, autonomy=agent.autonomy_view())

    def adopt(self, agent: ProtoAgent, name: str = "默认") -> Optional[dict]:
        """收编一个既有 Agent（单 Agent 旧布局兼容，幂等：同一目录只收一次）。"""
        directory = agent.data_dir.resolve()
        with self._lock:
            reg = self._load_registry()
            if any(self._dir_for(row).resolve() == directory
                   or (row.get("archive_dir") and (self.root / row["archive_dir"]).resolve() == directory)
                   for row in reg.get("archived", [])):
                return None
            existing = next((a for a in reg["agents"]
                             if self._dir_for(a).resolve() == directory), None)
            if existing is not None:
                agent = self._cache.get(existing["id"], agent)
                self._cache[existing["id"]] = self._connect(agent, existing)
                return existing
            agent_id = self._allocate_id(reg, directory.name)
            info = {"id": agent_id,
                    "name": name, "created_at": time.time(), "paused": False,
                    "external_dir": str(directory)}
            self._connect(agent, info)
            reg["agents"].append(info)
            reg["current"] = reg["current"] or info["id"]
            self._save_registry(reg)
            self._cache[info["id"]] = agent
            return info

    def _dir_for(self, info: dict) -> Path:
        return Path(info.get("external_dir") or self.root / "agents" / info["id"])

    def _build(self, info: dict, persona: str = "", initial_autonomy=None) -> ProtoAgent:
        agent = ProtoAgent(data_dir=self._dir_for(info), llm=self._llm,
                           fast_llm=self._fast, webtools=copy(self._webtools),
                           persona=persona or None, initial_autonomy=initial_autonomy)
        return self._connect(agent, info)

    def _connect(self, agent: ProtoAgent, info: dict) -> ProtoAgent:
        agent.bind_fleet(self.blackboard, self._fleet_status_for(info["id"]), info["id"], self.runs)
        if self.models is not None:
            agent.bind_model_catalog(self.models)
        agent.set_fleet_paused(bool(info.get("paused")))
        return agent

    def info(self, agent_id: str) -> Optional[dict]:
        reg = self._load_registry()
        return next((a for a in reg["agents"] if a["id"] == agent_id), None)

    def rename(self, agent_id: str, name: str) -> Optional[dict]:
        if not isinstance(name, str):
            raise ValueError("invalid_agent_name")
        name = name.strip()
        if not 1 <= len(name) <= 40 or any(unicodedata.category(char) in {"Cc", "Cs", "Zl", "Zp"}
                                          for char in name):
            raise ValueError("invalid_agent_name")
        with self._lock:
            reg = self._load_registry()
            info = next((row for row in reg["agents"] if row["id"] == agent_id), None)
            if info is None:
                return None
            if info["name"] != name:
                info["name"] = name
                self._save_registry(reg)
            return {"id": info["id"], "name": info["name"]}

    def get(self, agent_id: str) -> Optional[ProtoAgent]:
        with self._lock:
            if agent_id in self._cache:
                return self._cache[agent_id]
            info = self.info(agent_id)
            if info is None:
                return None
            agent = self._build(info)
            self._cache[agent_id] = agent
            return agent

    def current_id(self) -> Optional[str]:
        return self._load_registry()["current"]

    def set_current(self, agent_id: str) -> None:
        with self._lock:
            reg = self._load_registry()
            if any(a["id"] == agent_id for a in reg["agents"]):
                reg["current"] = agent_id
                self._save_registry(reg)

    def agent_for_request(self, agent_id: Optional[str]) -> Optional[ProtoAgent]:
        return self.get(agent_id) if agent_id else self.get(self.current_id())

    @contextmanager
    def active_access(self, agent: ProtoAgent):
        agent_id = agent.fleet_id
        with self._lock:
            if self._cache.get(agent_id) is not agent:
                raise CoordinationError("no_such_agent", 404)
            self._active_accesses[agent_id] = self._active_accesses.get(agent_id, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                self._active_accesses[agent_id] -= 1
                if not self._active_accesses[agent_id]:
                    del self._active_accesses[agent_id]

    def pause(self, agent_id: str, on: bool = True) -> bool:
        if type(on) is not bool:
            raise ValueError("on_must_be_boolean")
        with self._lock:
            reg = self._load_registry()
            info = next((row for row in reg["agents"] if row["id"] == agent_id), None)
            if info is None:
                return False
            self._set_paused(reg, [info], on)
        self.runs.notify()
        return True

    def pause_all(self, on: bool) -> dict:
        if type(on) is not bool:
            raise ValueError("on_must_be_boolean")
        with self._lock:
            reg = self._load_registry()
            rows = reg["agents"]
            result = {"total": len(rows), "changed": self._set_paused(reg, rows, on)}
        if result["changed"]:
            self.runs.notify()
        return result

    def _set_paused(self, reg: dict, rows: List[dict], on: bool) -> int:
        changed = 0
        for info in rows:
            if bool(info.get("paused")) != on:
                info["paused"] = on
                changed += 1
        if changed:
            self._save_registry(reg)
        for info in rows:
            agent = self._cache.get(info["id"])
            if agent is not None:
                agent.set_fleet_paused(on)
        return changed

    def archive(self, agent_id: str) -> bool:
        return self._try_archive(agent_id) is not None

    def archive_all(self, agent_ids: List[str]) -> dict:
        targets = self._load_registry()["agents"]
        if {row["id"] for row in targets} != set(agent_ids):
            raise CoordinationError("fleet_membership_changed")
        archived, skipped = [], []
        for row in targets:
            try:
                result = self._try_archive(row["id"])
                if result is not None:
                    archived.append(result)
                    continue
                error = "no_such_agent"
            except CoordinationError as exc:
                error = str(exc)
            except OSError:
                logging.exception("Could not save archive registration: %s", row["id"])
                error = "archive_write_failed"
            skipped.append({"id": row["id"], "name": row["name"], "error": error})
        return {"total": len(targets), "archived": archived, "skipped": skipped}

    def _try_archive(self, agent_id: str) -> Optional[dict]:
        lock = self.ops_lock(agent_id)
        if not lock.acquire(blocking=False):
            raise CoordinationError("agent_busy")
        try:
            result = self._archive(agent_id)
        finally:
            lock.release()
        if result is not None:
            self.runs.notify()
        return result

    def _archive(self, agent_id: str) -> Optional[dict]:
        with self._lock:
            reg = self._load_registry()
            info = next((a for a in reg["agents"] if a["id"] == agent_id), None)
            if info is None:
                return None
            agent = self._cache.get(agent_id)
            if agent is not None and not agent.dialogue_lock.acquire(blocking=False):
                raise CoordinationError("chat_in_progress")
            try:
                if self._active_accesses.get(agent_id):
                    raise CoordinationError("agent_busy")
                entry = dict(info, archived_at=time.time())
                if not info.get("external_dir"):
                    entry["archive_dir"] = f"archive/{agent_id}-{time.time_ns()}"
                reg.setdefault("archived", []).append(entry)
                reg["agents"] = [a for a in reg["agents"] if a["id"] != agent_id]
                if reg["current"] == agent_id:
                    reg["current"] = reg["agents"][0]["id"] if reg["agents"] else None
                self._save_registry(reg)
                if agent is not None:
                    agent.dialogue_closed = True
                self._cache.pop(agent_id, None)
            finally:
                if agent is not None:
                    agent.dialogue_lock.release()
        self.runs.activity.forget(agent_id)
        result = {"id": agent_id, "name": info["name"]}
        if "archive_dir" not in entry:
            return dict(result, storage="in_place", note="external_directory")
        try:
            self._dir_for(info).rename(self.root / entry["archive_dir"])
        except OSError:
            logging.exception("Archived but could not move Agent data: %s", agent_id)
            return dict(result, storage="in_place", note="archive_move_failed")
        return dict(result, storage="archive")

    # ---- 活动观测：它现在在干什么 ----
    ACTIVITY_LABELS = [
        ("web_fetch", "学习中"), ("prediction_error", "调查预测落空"),
        ("goal_pushed", "生成目标"), ("reflection_write", "反思中"),
        ("evolution_merge", "自我进化"), ("verification_failed", "补证据"),
        ("task", "做事中"), ("modulation", "被反馈塑造"),
        ("memory_forget", "遗忘指令"), ("approval", "获批准"), ("denial", "被拒绝"),
    ]

    @classmethod
    def activity_of(cls, events: List[dict]) -> dict:
        """由最近审计事件推导当前活动状态（人可读）。"""
        if not events:
            return {"status": "待命", "detail": "尚无活动记录"}
        latest = events[-1]
        status = "交互中"
        for event_type, label in cls.ACTIVITY_LABELS:
            if event_type in latest["event_type"]:
                status = label
                break
        return {"status": status, "detail": str(latest.get("payload", ""))[:80],
                "at": latest.get("ts")}

    def ops_lock(self, agent_id: str):
        """M50: per-agent 单写者锁——一个 agent 的长链不再挡住其他 agent。"""
        with self._ops_locks_guard:
            lock = self._ops_locks.get(agent_id)
            if lock is None:
                import threading as _th
                lock = _th.Lock()
                self._ops_locks[agent_id] = lock
            return lock

    def _fleet_status_for(self, agent_id: str):
        """M47: 给每个 agent 一份'其他成员在干什么'的只读快照（闭包惰性求值）。"""
        def snapshot() -> list:
            rows = []
            for row in self.list():
                if row.get("id") == agent_id:
                    continue
                act = row.get("activity") or {}
                rows.append({
                    "name": row.get("name") or row.get("id"),
                    "status": act.get("status", "?"),
                    "detail": f"{act.get('detail', '')[:60]}"
                              f"｜待审批 {row.get('pending_proposals', 0)}"
                              f"｜目标 {row.get('goals_active', 0)} 个"
                              if act else "",
                })
            return rows
        return snapshot

    # ---- 观测与驱动 ----
    def list(self) -> List[dict]:
        return self._list_members(self._load_registry()["agents"])

    def fleet_view(self) -> dict:
        reg = self._load_registry()
        return {"agents": self._list_members(reg["agents"]), "current": reg["current"]}

    def _list_members(self, members: List[dict]) -> List[dict]:
        rows = []
        for info in members:
            row = dict(info)
            try:
                agent = self._cache.get(info["id"])
                if agent is not None:
                    row.update(self.snapshot_of(agent))
                else:
                    row.update(self._stored_view(info))
            except FileNotFoundError:
                if self.info(info["id"]) is not None:
                    raise
            rows.append(row)
        return rows

    def _stored_view(self, info: dict) -> dict:
        directory = self._dir_for(info)
        if not directory.is_dir():
            return {}
        with self._lock:
            if self.info(info["id"]) is None:
                return {}
            store = create_store(directory)
        profile = store.get("config", "persona") or {}
        initial = AutonomyPolicy() if AgentProfile.from_doc(profile) is not None else None
        policy, _ = load_policy(store, initial)
        result = {"autonomy": dict(policy.to_dict(), blocked_reason=policy.blocker(paused=info.get("paused"))),
                  "persona": profile.get("text", "") if isinstance(profile, dict) else ""}
        if info.get("paused"):
            result["activity"] = {"status": "已暂停", "detail": "暂停中（心跳停止，记忆/目标保留）"}
        return result

    @staticmethod
    def snapshot_of(agent: ProtoAgent) -> dict:
        goals = agent.goal_tree.all_active()
        progress = (sum(g.progress for g in goals) / len(goals)) if goals else 0.0
        activity = AgentManager.activity_of(agent.audit_tail(3))
        autonomy = agent.autonomy_view()
        if autonomy["blocked_reason"] in ("muted", "fleet_paused"):
            activity = dict(activity, status="已暂停",
                            detail="暂停中（心跳停止，记忆/目标保留）")
        return {
            "activity": activity,
            "last_events": [{"event_type": e["event_type"], "ts": e.get("ts")}
                            for e in agent.audit_tail(6)],
            "persona": agent.persona,
            "priority": agent.state.owner_priority,
            "emotion": dict(agent.state.emotion),
            "desires": {name: spec["value"] for name, spec
                        in agent.state.desires.items()},
            "memory_total": len(agent.memory),
            "pending_proposals": len(agent.pending_proposals()),
            "goals_active": len(goals),
            "goals_progress": round(progress, 3),
            "autonomy": autonomy,
            "muted": agent.kernel.muted,
        }

    def tick_all(self) -> None:
        import logging
        for info in self._load_registry()["agents"]:
            if info.get("paused"):
                continue
            try:
                agent = self.get(info["id"])
                if agent is None:
                    continue
                # M50：逐 agent try-lock——A 的长链不再饿死 B 的节律
                lock = self.ops_lock(info["id"])
                if not lock.acquire(timeout=0.2):
                    continue
                try:
                    if not agent.dialogue_closed:
                        agent.maybe_proactive()
                finally:
                    lock.release()
            except Exception:
                # 失败不致命但必须留痕——静默吞异常曾掩盖整段自主性停摆
                import logging
                logging.exception("heartbeat tick 失败 agent=%s", info.get("id"))

    def enqueue_ticks(self) -> None:
        for info in self._load_registry()["agents"]:
            if info.get("paused"):
                continue
            try:
                agent = self.get(info["id"])
                if agent is None:
                    continue
                with self.active_access(agent):
                    if not agent.autonomy_blocker(suggestions=True) and agent.heartbeat_ready():
                        self.runs.enqueue(info["id"], "tick")
            except CoordinationError as exc:
                if exc.status not in (404, 429):
                    logging.exception("Failed to enqueue heartbeat: %s", info["id"])
            except Exception:
                logging.exception("Failed to enqueue heartbeat: %s", info["id"])
