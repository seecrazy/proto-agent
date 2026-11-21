# -*- coding: utf-8 -*-
"""M22: 舰队管理 CLI——python3 -m proto.agents <命令>

  list                        列出全部 Agent（含观测快照）
  create --name N [--persona P] [--priority R]
  select <id>                 切换当前 Agent（控制台无头时默认）
  pause <id> / resume <id>    暂停/恢复自主行为
  archive <id> --yes          归档（数据保留 archive/）
根目录：--root 或 PROTO_FLEET_ROOT，默认 ./proto_data
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from proto.config import load_default_env

DEFAULT_ROOT = "./proto_data"


def _manager(root: str):
    from proto.llm.client import create_llm_stack
    from proto.runtime.manager import AgentManager
    stack = create_llm_stack()
    manager = AgentManager(root=Path(root), llm=stack["main"] if stack else None,
                           fast_llm=stack["fast"] if stack else None)
    manager.enable_model_updates()
    return manager


def main(argv=None) -> int:
    load_default_env()
    parser = argparse.ArgumentParser(prog="proto.agents",
                                     description="PROTO Agent 舰队管理")
    parser.add_argument("--root", default=None,
                        help="舰队根目录（默认 $PROTO_FLEET_ROOT 或 ./proto_data）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p_create = sub.add_parser("create")
    p_create.add_argument("--name", required=True)
    p_create.add_argument("--persona", default="")
    p_create.add_argument("--priority", default="")
    p_select = sub.add_parser("select")
    p_select.add_argument("id")
    p_pause = sub.add_parser("pause")
    p_pause.add_argument("id")
    p_resume = sub.add_parser("resume")
    p_resume.add_argument("id")
    p_arch = sub.add_parser("archive")
    p_arch.add_argument("id")
    p_arch.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)

    import os
    root = args.root or os.environ.get("PROTO_FLEET_ROOT", DEFAULT_ROOT)
    mgr = _manager(root)

    from proto import ui

    if args.cmd == "list":
        rows = mgr.list()
        current = mgr.current_id()
        print(ui.kv_table([("根目录", root, None), ("当前", current or "（空）", ui.cyan)]))
        for a in rows:
            mark = " ←当前" if a["id"] == current else ""
            paused = " [已暂停]" if a.get("paused") else ""
            des = a.get("desires") or {}
            line = (f"{a['id']}  {a['name']}{mark}{paused}  "
                    f"人设={a.get('persona', '（未激活）')}  "
                    f"记忆={a.get('memory_total', '-')} 待批={a.get('pending_proposals', '-')} "
                    f"目标={a.get('goals_active', '-')}")
            print(ui.dim(line) if a.get("paused") else line)
        return 0
    if args.cmd == "create":
        info = mgr.create(name=args.name, persona=args.persona,
                          priority=args.priority)
        print(ui.ok(f"已创建 {info['name']}（{info['id']}）"))
        return 0
    if args.cmd == "select":
        if mgr.info(args.id) is None:
            print(ui.fail(f"不存在：{args.id}")); return 1
        mgr.set_current(args.id)
        print(ui.ok(f"当前 Agent → {args.id}")); return 0
    if args.cmd in ("pause", "resume"):
        if mgr.info(args.id) is None:
            print(ui.fail(f"不存在：{args.id}")); return 1
        mgr.pause(args.id, on=(args.cmd == "pause"))
        print(ui.ok(f"{'已暂停' if args.cmd == 'pause' else '已恢复'}：{args.id}"))
        return 0
    if args.cmd == "archive":
        if not args.yes:
            print(ui.warn("归档需 --yes 确认（数据保留在 archive/）")); return 1
        ok = mgr.archive(args.id)
        print(ui.ok(f"已归档：{args.id}") if ok else ui.fail(f"不存在：{args.id}"))
        return 0 if ok else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
