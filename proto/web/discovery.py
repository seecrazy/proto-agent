from __future__ import annotations

import hmac
import json
import logging
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

DESKTOP_DIR = ".proto-console"
DESKTOP_RECORD = "endpoint.json"
DESKTOP_CHALLENGE = "X-Proto-Desktop-Challenge"
DESKTOP_PROOF = "X-Proto-Desktop-Proof"


def desktop_proof(key: bytes, challenge: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", challenge or ""):
        return ""
    return hmac.digest(key, challenge.encode("ascii"), "sha256").hex()


def _publish(directory: Path, server) -> Path:
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError("unsafe console discovery directory")
    record = directory / DESKTOP_RECORD
    with tempfile.NamedTemporaryFile(dir=directory, mode="w", encoding="utf-8", delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump({"version": 1, "data_dir": str(directory.parent.resolve()),
                       "pid": os.getpid(), "port": server.port,
                       "key": server.desktop_key.hex(), "token": server.token}, file)
            file.flush()
            os.replace(temporary, record)
        finally:
            temporary.unlink(missing_ok=True)
    return record


@contextmanager
def console_discovery(data_root: Path, server):
    record = None
    if os.name == "posix":
        try:
            record = _publish(data_root / DESKTOP_DIR, server)
        except OSError:
            logging.warning("无法发布本机桌面连接记录，Web 仍可使用。")
    try:
        yield
    finally:
        if record is not None:
            try:
                record.unlink(missing_ok=True)
            except OSError:
                logging.warning("无法清除已失效的本机桌面连接记录。")
