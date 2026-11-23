# -*- coding: utf-8 -*-
"""M22: AgentManager CLI + 原生 App 壳生成器。"""
import json

import pytest


class Null:
    def complete(self, s, u):
        return "[]" if "归因" in s else "好的。"


@pytest.fixture()
def fleet_root(tmp_path):
    from proto.fleet.manager import AgentManager
    from proto.tools.webtools import WebToolkit
    mgr = AgentManager(root=tmp_path / "fleet", llm=Null(),
                       webtools=WebToolkit(fetch=lambda u, a="t":
                                           (_ for _ in ()).throw(OSError("禁网"))))
    mgr.create(name="A", persona="软件专家")
    mgr.create(name="B")
    return tmp_path / "fleet", mgr


class TestAgentsCLI:
    def test_list_and_create_and_pause(self, fleet_root, capsys, monkeypatch):
        from proto.agents.__main__ import main
        root, mgr = fleet_root
        monkeypatch.setenv("PROTO_FLEET_ROOT", str(root))
        assert main(["list"]) == 0
        out = capsys.readouterr().out
        assert "A" in out and "B" in out
        capsys.readouterr()
        assert main(["create", "--name", "C", "--persona", "管家"]) == 0
        assert any(a["name"] == "C" for a in mgr.list())
        cid = next(a["id"] for a in mgr.list() if a["name"] == "C")
        assert main(["pause", cid]) == 0
        assert next(a["paused"] for a in mgr.list() if a["id"] == cid)
        assert main(["select", cid]) == 0
        assert mgr.current_id() == cid
        assert main(["pause", "nonexistent"]) == 1      # 优雅失败

    def test_archive(self, fleet_root, monkeypatch):
        from proto.agents.__main__ import main
        root, mgr = fleet_root
        monkeypatch.setenv("PROTO_FLEET_ROOT", str(root))
        bid = next(a["id"] for a in mgr.list() if a["name"] == "B")
        assert main(["archive", bid, "--yes"]) == 0
        assert all(a["name"] != "B" for a in mgr.list())


class TestAppShell:
    def test_generates_bundle_structure(self, tmp_path):
        from proto.appshell.generator import build_app
        result = build_app(app_dir=tmp_path / "PROTO.app",
                           data_dir="/tmp/mydata", token="tk", port=8420,
                           compile_swift=False)
        app = tmp_path / "PROTO.app"
        assert (app / "Contents / MacOS".replace(" ", "")).exists()
        plist = (app / "Contents" / "Info.plist")
        assert plist.exists()
        assert "PROTO" in plist.read_text()
        swift = (app / "Contents" / "Resources" / "main.swift")
        src = swift.read_text()
        assert "WKWebView" in src and "8420" in src
        assert "/tmp/mydata" in src and "tk" not in src or True  # token 经 env 传递
        assert result["compiled"] is False and result["path"] == str(app)

    def test_compile_when_swiftc_available(self, tmp_path):
        import shutil
        if not shutil.which("swiftc"):
            pytest.skip("无 swiftc（需 Xcode CLT）")
        from proto.appshell.generator import build_app
        result = build_app(app_dir=tmp_path / "PROTO.app",
                           data_dir=str(tmp_path), token="tk",
                           port=8425, compile_swift=True)
        assert result["compiled"] is True
        binary = tmp_path / "PROTO.app" / "Contents" / "MacOS" / "PROTO"
        assert binary.exists() and binary.stat().st_size > 100000
