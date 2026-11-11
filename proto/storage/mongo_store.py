# -*- coding: utf-8 -*-
"""MongoDB DocumentStore 适配器 + 存储工厂。

接入条件：
  1. 一个可达的 MongoDB（本机 mongod / Docker / Atlas，URI 形如
     mongodb://localhost:27017 或 mongodb+srv://user:pass@xxx.mongodb.net）；
  2. pip install pymongo（或 pip install -e .[mongo]）。

启用方式（环境变量，proto.run / proto.web / ProtoAgent 均生效）：
  PROTO_STORE=mongo
  PROTO_MONGO_URI=mongodb://localhost:27017
  PROTO_MONGO_DB=proto          # 可选，默认 proto

未设置时一切照旧使用文件存储——文件与 Mongo 两个后端过同一套
DocumentStore 契约测试（test_store.py 的行为在两后端上一致）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, List, Optional

from proto.storage.store import DocumentStore, FileSystemStore


class MongoStore(DocumentStore):
    """pymongo 实现。MongoClient 懒连接：构造不触网，首个操作才连。"""

    def __init__(self, uri: str, db_name: str = "proto"):
        try:
            from pymongo import MongoClient
        except ImportError as exc:
            raise ImportError(
                "PROTO_STORE=mongo 需要 pymongo：pip install pymongo "
                "（或 pip install -e .[mongo]）") from exc
        self._client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        self._db = self._client[db_name]

    def put(self, collection: str, doc_id: str, doc: dict) -> None:
        self._db[collection].replace_one(
            {"_id": doc_id}, {**doc, "_id": doc_id}, upsert=True)

    def get(self, collection: str, doc_id: str) -> Optional[dict]:
        raw = self._db[collection].find_one({"_id": doc_id})
        if raw is None:
            return None
        raw.pop("_id", None)
        return raw

    def delete(self, collection: str, doc_id: str) -> None:
        self._db[collection].delete_one({"_id": doc_id})

    def find(self, collection: str, predicate: Callable[[dict], bool]) -> List[dict]:
        rows = []
        for raw in self._db[collection].find():
            raw.pop("_id", None)
            if predicate(raw):
                rows.append(raw)
        return rows


def create_store(data_dir: Path) -> DocumentStore:
    """存储工厂：按环境变量选择后端。默认文件存储（零依赖）。"""
    if os.environ.get("PROTO_STORE", "").lower() == "mongo":
        uri = os.environ.get("PROTO_MONGO_URI", "mongodb://localhost:27017")
        db_name = os.environ.get("PROTO_MONGO_DB", "proto")
        return MongoStore(uri, db_name)
    return FileSystemStore(Path(data_dir) / "data")
