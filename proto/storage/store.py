# -*- coding: utf-8 -*-
"""L2/L3 持久化抽象：DocumentStore。

接口按文档数据库语义设计（collection / doc_id / get / put / find），
默认文件实现零依赖；MongoDB-compatible 后端可原位替换。
"""
from __future__ import annotations

import json
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


class FileSystemStore(DocumentStore):
    """基于目录 + JSON 文件的实现：<root>/<collection>/<doc_id>.json"""

    def __init__(self, root: Path):
        self._root = Path(root)
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
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)

    def get(self, collection: str, doc_id: str) -> Optional[dict]:
        path = self._path(collection, doc_id)
        with self._lock:
            if not path.exists():
                return None
            return json.loads(path.read_text(encoding="utf-8"))

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
