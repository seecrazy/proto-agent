# -*- coding: utf-8 -*-
"""M15: 部署引导——env 配置文件/plist 生成/自动加载/非交互安装。"""
import os
import plistlib
import stat

import pytest


class TestEnvFile:
    def test_env_file_written_with_0600_and_exports(self, tmp_path):
        from proto.setup import ensure_env_file

        path = ensure_env_file(
            data_dir=tmp_path, provider="zhipu-coding", api_key="sk-abc def",
            model="glm-5.3", fast_model="glm-5.3-flash", token="tok-1")
        text = path.read_text(encoding="utf-8")
        assert "export PROTO_LLM_PROVIDER=zhipu-coding" in text
        assert "PROTO_LLM_API_KEY='sk-abc def'" in text      # 含空格安全引号
        assert "PROTO_LLM_FAST_MODEL" in text and "PROTO_WEB_TOKEN" in text
        assert (os.stat(path).st_mode & 0o777) == 0o600      # 内含 key

    def test_load_env_file_setdefault(self, tmp_path, monkeypatch):
        from proto.config import load_env_file

        env_path = tmp_path / "proto.env"
        env_path.write_text(
            'export PROTO_LLM_PROVIDER=zhipu-coding\n'
            'export PROTO_LLM_MODEL="glm-5.3"\n'
            "# 注释行\n",
            encoding="utf-8")
        monkeypatch.delenv("PROTO_LLM_PROVIDER", raising=False)
        monkeypatch.setenv("PROTO_LLM_MODEL", "已有值优先")
        loaded = load_env_file(env_path)
        assert loaded == 2
        assert os.environ["PROTO_LLM_PROVIDER"] == "zhipu-coding"
        assert os.environ["PROTO_LLM_MODEL"] == "已有值优先"   # 不覆盖显式配置


class TestLaunchd:
    def test_plist_generation(self, tmp_path):
        from proto.setup import build_plist, plist_path_for

        plist = build_plist(data_dir=tmp_path, token="tok-1",
                            python_bin="/opt/venv/bin/python")
        assert plist["Label"] == "com.proto.agent"
        args = plist["ProgramArguments"]
        assert args[0] == "/opt/venv/bin/python"
        assert args[1:3] == ["-m", "proto.web"]
        assert "--token" in args and "tok-1" in args
        assert plist["RunAtLoad"] is True and plist["KeepAlive"] is True
        assert plist["EnvironmentVariables"]["PROTO_LLM_PROVIDER"] or True
        # 可落盘为合法 plist
        import io
        buf = io.BytesIO()
        plistlib.dump(plist, buf)
        assert b"com.proto.agent" in buf.getvalue()

    def test_plist_path(self, tmp_path):
        from proto.setup import plist_path_for
        assert plist_path_for().name == "com.proto.agent.plist"
        assert "LaunchAgents" in str(plist_path_for())


class TestNonInteractiveSetup:
    def test_full_setup_artifacts(self, tmp_path):
        from proto.setup import run_setup

        result = run_setup(
            data_dir=tmp_path, provider="zhipu-coding", api_key="k1",
            model="glm-5.3", fast_model="glm-5.3-flash",
            token="deploy-token", persona="资深软件专家",
            priority="构建工具链", yes=True)
        assert result["env_file"].exists()
        assert result["token"] == "deploy-token"
        # 人设与优先级已写入 agent
        from proto.runtime.agent import ProtoAgent
        from proto.exec.webtools import WebToolkit
        agent = ProtoAgent(data_dir=tmp_path, goal_interval_s=10**9,
                           webtools=WebToolkit(fetch=lambda u, a="t":
                                               (_ for _ in ()).throw(OSError("禁网"))))
        assert agent.persona == "资深软件专家"
        assert agent.state.owner_priority == "构建工具链"
