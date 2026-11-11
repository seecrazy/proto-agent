# -*- coding: utf-8 -*-
"""阶段 6：scratch 工作区（设计 §8，R9）。

静默执行的一切产出限定在 scratch/；晋升到 visible/ 必须持已批准的审批记录。
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import List, Optional

from proto.kernel.audit import AuditEvent, AuditSpine


def _safe_rel(rel_path: str) -> Path:
    parts = Path(rel_path).parts
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"非法相对路径: {rel_path!r}")
    return Path(*parts)


class ScratchWorkspace:
    def __init__(self, root: Path, audit: AuditSpine = None):
        self._root = Path(root)
        self._scratch = self._root / "scratch"
        self._visible = self._root / "visible"
        self._scratch.mkdir(parents=True, exist_ok=True)
        self._visible.mkdir(parents=True, exist_ok=True)
        self._audit = audit

    def write(self, rel_path: str, content: str) -> Path:
        """静默阶段写入——永远落在 scratch 内。"""
        rel = _safe_rel(rel_path)
        path = self._scratch / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def promote(self, rel_path: str, approval) -> Path:
        """晋升到可见工作区：必须持 granted 的审批记录（设计 §8）。"""
        if approval is None or not getattr(approval, "granted", False):
            raise PermissionError("晋升需要已批准的审批记录")
        rel = _safe_rel(rel_path)
        src = self._scratch / rel
        if not src.exists():
            raise FileNotFoundError(f"scratch 中不存在: {rel_path}")
        dst = self._visible / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        if self._audit is not None:
            self._audit.append(AuditEvent.PROMOTION, {"path": rel_path})
        return dst

    def _list(self, base: Path) -> List[str]:
        return sorted(str(p.relative_to(base)) for p in base.rglob("*") if p.is_file())

    def list_scratch(self) -> List[str]:
        return self._list(self._scratch)

    def list_visible(self) -> List[str]:
        return self._list(self._visible)
