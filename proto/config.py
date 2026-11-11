# -*- coding: utf-8 -*-
"""M15: 配置文件加载——proto.env（部署向导产出，0600）。

查找顺序：./proto_data/proto.env → ./proto.env。
已显式设置的环境变量优先，文件不覆盖——本地调试可随时覆盖部署配置。
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_FILE_CANDIDATES = ("./proto_data/proto.env", "./proto.env")


def load_env_file(path: Path) -> int:
    """解析 export KEY=VALUE 行（支持引号/注释），setdefault 进环境。"""
    loaded = 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or not line.startswith("export "):
            continue
        body = line[len("export "):].strip()
        key, sep, value = body.partition("=")
        if not sep or not key.strip():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)
        loaded += 1
    return loaded


def load_default_env() -> int:
    for candidate in ENV_FILE_CANDIDATES:
        path = Path(candidate)
        if path.exists():
            return load_env_file(path)
    return 0
