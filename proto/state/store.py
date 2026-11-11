# -*- coding: utf-8 -*-
"""兼容旧路径的转发模块。DocumentStore 已提层为中立基础设施：proto/storage/store.py。"""
from proto.storage.store import DocumentStore, FileSystemStore  # noqa: F401
