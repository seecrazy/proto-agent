# -*- coding: utf-8 -*-
"""L2/L3 持久化抽象：DocumentStore。

接口按文档数据库语义设计（collection / doc_id / get / put / find），
默认文件实现零依赖；MongoDB-compatible 后端可原位替换。
"""
from __future__ import annotations

import json
import os
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Dict, List, Optional


class DocumentStore(ABC):
    """文档存储接口。所有实现必须线程安全（单进程内）。"""

    @abstractmethod
    def put(self, collection: str, doc_id: str, doc: dict) -> None:
        """写入或整体覆盖一个文档。"""

    @abstractmethod
    def get(self, collection: str, doc_id: str) -> Optional[dict]:
        """读取文档；不存在返回 None。"""

    @abstractmethod
    def delete(self, collection: str, doc_id: str) -> None:
        """删除文档；不存在时幂等。"""

    @abstractmethod
    def find(self, collection: str, predicate: Callable[[dict], bool]) -> List[dict]:
        """返回集合内满足谓词的全部文档，任意稳定顺序。"""

    def revision(self, collection: str) -> Optional[object]:
        """集合修订标识；用于上层缓存失效。None 表示不支持。"""
        return None


class FileSystemStore(DocumentStore):
    """基于目录 + JSON 文件的实现：<root>/<collection>/<doc_id>.json"""

    def __init__(self, root: Path):
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        try:
            self._root.chmod(0o700)
        except OSError:
            pass
        self._lock = threading.Lock()

    def _path(self, collection: str, doc_id: str) -> Path:
        for name in (collection, doc_id):
            if "/" in name or name in ("", ".", ".."):
                raise ValueError(f"非法 collection/doc_id: {name!r}")
        return self._root / collection / (doc_id + ".json")

    def put(self, collection: str, doc_id: str, doc: dict) -> None:
        path = self._path(collection, doc_id)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                path.parent.chmod(0o700)
            except OSError:
                pass
            tmp = path.with_suffix(f".json.{os.getpid()}.tmp")  # 每进程独立tmp防竞争
            text = json.dumps(doc, ensure_ascii=False, indent=2)
            tmp.write_text(text, encoding="utf-8")
            try:
                tmp.chmod(0o600)
            except OSError:
                pass
            try:
                os.replace(tmp, path)
            except FileNotFoundError:
                # 多进程并发写同 doc：另一方已 replace——重试一次（内容幂等则成功）
                tmp.write_text(text, encoding="utf-8")
                tmp.chmod(0o600)
                os.replace(tmp, path)
            try:
                path.chmod(0o600)
            except OSError:
                pass

    def get(self, collection: str, doc_id: str) -> Optional[dict]:
        path = self._path(collection, doc_id)
        with self._lock:
            if not path.exists():
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
            if value is None:
                raise ValueError("null_document")
            return value

    def delete(self, collection: str, doc_id: str) -> None:
        path = self._path(collection, doc_id)
        with self._lock:
            if path.exists():
                path.unlink()

    def find(self, collection: str, predicate: Callable[[dict], bool]) -> List[dict]:
        dir_path = self._root / collection
        with self._lock:
            if not dir_path.exists():
                return []
            docs = []
            for file in sorted(dir_path.glob("*.json")):
                doc = json.loads(file.read_text(encoding="utf-8"))
                if predicate(doc):
                    docs.append(doc)
            return docs

    def revision(self, collection: str) -> Optional[object]:
        dir_path = self._root / collection
        try:
            names = sorted(p.name for p in dir_path.glob("*.json"))
            latest = max((p.stat().st_mtime_ns for p in dir_path.glob("*.json")),
                         default=0)
            return (len(names), latest, names[-1] if names else "")
        except OSError:
            return (0, 0, "")
