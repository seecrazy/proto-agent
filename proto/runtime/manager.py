# -*- coding: utf-8 -*-
"""M21: 多自主 Agent 管理——创建/管理/观测一支 Agent 舰队。

一个主人（owner），多个 Agent：各有独立身份（锚定/人设/记忆/目标树/预算），
共享 LLM 配置。注册表在 root/registry.json；数据在各 agents/<id>/；
归档不删除（移入 archive/，可恢复）。心跳统一驱动全舰队（暂停者跳过）。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from proto.runtime.agent import ProtoAgent
from proto.storage.mongo_store import create_store
from proto.runtime.collab import Blackboard

REGISTRY = "registry.json"


class AgentManager:
    def __init__(self, root: Path, llm=None, fast_llm=None, webtools=None,
                 blackboard=None):
        self.root = Path(root)
        try:
            self.root.chmod(0o700)
        except OSError:
            pass
        (self.root / "agents").mkdir(parents=True, exist_ok=True)
        (self.root / "archive").mkdir(parents=True, exist_ok=True)
        self._llm = llm
        self._fast = fast_llm if fast_llm is not None else llm
        self._webtools = webtools
        self.blackboard = blackboard or Blackboard(create_store(self.root))
        self._cache: Dict[str, ProtoAgent] = {}
        self._lock = threading.Lock()
        self._registry_path = self.root / REGISTRY

    # ---- 注册表 ----
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

    # ---- 生命周期 ----
    def create(self, name: str, persona: str = "", priority: str = "") -> dict:
        with self._lock:
            reg = self._load_registry()
            info = {"id": uuid.uuid4().hex[:10], "name": name.strip()[:40] or "Agent",
                    "created_at": time.time(), "paused": False}
            reg["agents"].append(info)
            reg["current"] = reg["current"] or info["id"]
            self._save_registry(reg)
        agent = self._build(info["id"], persona, priority)
        return info

    def adopt(self, agent: ProtoAgent, name: str = "默认") -> dict:
        """收编一个既有 Agent（单 Agent 旧布局兼容，幂等：同一目录只收一次）。"""
        with self._lock:
            reg = self._load_registry()
            existing = next((a for a in reg["agents"]
                             if a.get("external_dir") == str(agent.data_dir)), None)
            if existing is not None:
                self._cache[existing["id"]] = agent
                return existing                    # 重启再收编 → 复用
            info = {"id": agent.data_dir.name or uuid.uuid4().hex[:10],
                    "name": name, "created_at": time.time(), "paused": False,
                    "external_dir": str(agent.data_dir)}
            reg["agents"].append(info)
            reg["current"] = reg["current"] or info["id"]
            self._save_registry(reg)
        self._cache[info["id"]] = agent
        return info

    def _dir_for(self, info: dict) -> Path:
        return Path(info.get("external_dir") or self.root / "agents" / info["id"])

    def _build(self, agent_id: str, persona: str, priority: str) -> ProtoAgent:
        info = self.info(agent_id)
        agent = ProtoAgent(data_dir=self._dir_for(info), llm=self._llm,
                           fast_llm=self._fast, webtools=self._webtools,
                           persona=persona or None,
                           blackboard=self.blackboard,
                           fleet_status=self._fleet_status_for(agent_id))
        if priority:
            agent.set_priority(priority)
        if info.get("paused"):
            agent.kernel.set_mute(True)   # 重启后恢复暂停态（registry 是权威）
        self._cache[agent_id] = agent
        return agent

    def info(self, agent_id: str) -> Optional[dict]:
        reg = self._load_registry()
        return next((a for a in reg["agents"] if a["id"] == agent_id), None)

    def get(self, agent_id: str) -> Optional[ProtoAgent]:
        with self._lock:
            if agent_id in self._cache:
                return self._cache[agent_id]
            info = self.info(agent_id)
            if info is None:
                return None
            return self._build(agent_id, "", "")

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

    def pause(self, agent_id: str, on: bool = True) -> None:
        with self._lock:
            reg = self._load_registry()
            for a in reg["agents"]:
                if a["id"] == agent_id:
                    a["paused"] = on
            self._save_registry(reg)
        agent = self._cache.get(agent_id)
        if agent is not None:
            agent.kernel.set_mute(on)

    def archive(self, agent_id: str) -> bool:
        """归档（不删除）：数据移入 archive/，可手工恢复。"""
        with self._lock:
            reg = self._load_registry()
            info = next((a for a in reg["agents"] if a["id"] == agent_id), None)
            if info is None:
                return False
            reg["agents"] = [a for a in reg["agents"] if a["id"] != agent_id]
            if reg["current"] == agent_id:
                reg["current"] = reg["agents"][0]["id"] if reg["agents"] else None
            self._save_registry(reg)
        self._cache.pop(agent_id, None)
        src = self._dir_for(info)
        if src.exists():
            shutil.move(str(src),
                        str(self.root / "archive" /
                            f"{agent_id}-{int(time.time())}"))
        return True

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
        rows = []
        reg = self._load_registry()
        for info in reg["agents"]:
            row = dict(info)
            agent = self._cache.get(info["id"])
            if agent is not None:
                row.update(self.snapshot_of(agent))
            elif info.get("paused"):
                row["activity"] = {"status": "已暂停",
                                   "detail": "暂停中（心跳停止，记忆/目标保留）"}
            rows.append(row)
        return rows

    @staticmethod
    def snapshot_of(agent: ProtoAgent) -> dict:
        goals = agent.goal_tree.all_active()
        progress = (sum(g.progress for g in goals) / len(goals)) if goals else 0.0
        activity = AgentManager.activity_of(agent.audit_tail(3))
        if agent.kernel.muted and activity["status"] not in ("已暂停",):
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
            "muted": agent.kernel.muted,
        }

    def tick_all(self) -> None:
        for info in self._load_registry()["agents"]:
            if info.get("paused"):
                continue
            try:
                agent = self.get(info["id"])
                if agent is not None:
                    agent.maybe_proactive()
            except Exception:
                # 失败不致命但必须留痕——静默吞异常曾掩盖整段自主性停摆
                import logging
                logging.exception("heartbeat tick 失败 agent=%s", info.get("id"))
