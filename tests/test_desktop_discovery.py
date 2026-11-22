import hashlib
import hmac
import json
import os
import stat
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import PurePosixPath

import pytest

from proto.backup import create_backup
from proto.runtime.agent import ProtoAgent
from proto.web.discovery import DESKTOP_CHALLENGE, DESKTOP_DIR, DESKTOP_PROOF, DESKTOP_RECORD, console_discovery
from proto.web.server import ProtoWebServer

pytestmark = pytest.mark.skipif(os.name != "posix", reason="private POSIX local discovery")


def test_desktop_descriptor_and_navigation_proof_share_the_web_instance(tmp_path):
    agent = ProtoAgent(tmp_path / "fleet")
    server = ProtoWebServer(agent, port=0, heartbeat_s=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    directory = agent.data_dir / DESKTOP_DIR
    record = directory / DESKTOP_RECORD
    base = f"http://127.0.0.1:{server.port}"

    def get(path, headers=None):
        return urllib.request.urlopen(urllib.request.Request(base + path, headers=headers or {}), timeout=5)

    try:
        with console_discovery(agent.data_dir, server):
            assert stat.S_IMODE(record.stat().st_mode) == 0o600
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
            endpoint = json.loads(record.read_text())
            assert endpoint == {"version": 1, "data_dir": str(agent.data_dir.resolve()),
                                "pid": os.getpid(), "port": server.port,
                                "key": server.desktop_key.hex(), "token": server.token}
            nonce = "01" * 32
            signature = hmac.new(bytes.fromhex(endpoint["key"]), nonce.encode(), hashlib.sha256).hexdigest()
            with get("/", {DESKTOP_CHALLENGE: nonce}) as response:
                assert response.headers[DESKTOP_PROOF] == signature
                assert response.read() == server.index_html
                assert all(secret not in str(response.headers) for secret in [endpoint["token"], endpoint["key"]])
            with get("/index.html", {DESKTOP_CHALLENGE: "invalid"}) as response:
                assert DESKTOP_PROOF not in response.headers
            with pytest.raises(urllib.error.HTTPError) as unauthorized:
                get("/api/agents", {DESKTOP_CHALLENGE: nonce})
            assert unauthorized.value.code == 401
            unauthorized.value.close()
            with get("/api/agents", {"X-Proto-Token": endpoint["token"]}) as response:
                assert json.load(response)["current"] == agent.fleet_id
            nested = agent.data_dir / ".fleet" / DESKTOP_DIR
            nested.mkdir(parents=True)
            (nested / DESKTOP_RECORD).write_text("transient")
            archive = tmp_path / "backup.zip"
            create_backup(agent.data_dir, archive)
            with zipfile.ZipFile(archive) as backup:
                assert "owner.key" in backup.namelist()
                assert not any(DESKTOP_DIR in PurePosixPath(name).parts for name in backup.namelist())
        assert not record.exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)


def test_existing_public_discovery_directory_is_not_used(tmp_path, caplog):
    directory = tmp_path / DESKTOP_DIR
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)
    with console_discovery(tmp_path, None):
        assert not (directory / DESKTOP_RECORD).exists()
    assert "Web 仍可使用" in caplog.text
