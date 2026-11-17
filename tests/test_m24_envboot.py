# -*- coding: utf-8 -*-
"""M24: 修复部署后离线——env 绑定数据目录加载 + 真实 WebToolkit + 离线归因提示。"""
import os


def test_load_env_with_explicit_data_dir(tmp_path, monkeypatch):
    from proto.config import load_default_env
    env = tmp_path / "proto.env"
    env.write_text("export PROTO_LLM_PROVIDER=zhipu-coding\n"
                   "export PROTO_LLM_API_KEY=sk-x\n", encoding="utf-8")
    monkeypatch.delenv("PROTO_LLM_PROVIDER", raising=False)
    monkeypatch.chdir("/")            # 任意 CWD
    loaded = load_default_env(data_dir=tmp_path)
    assert loaded == 2
    assert os.environ["PROTO_LLM_PROVIDER"] == "zhipu-coding"


def test_boot_agent_uses_real_webtools(tmp_path, monkeypatch):
    from proto.setup import _boot_agent
    (tmp_path / "proto.env").write_text(
        "export PROTO_LLM_PROVIDER=zhipu-coding\n"
        "export PROTO_LLM_API_KEY=sk-x\n", encoding="utf-8")
    agent = _boot_agent(tmp_path)
    assert agent.llm is not None                      # 不再离线
    # 真实 WebToolkit（可对外发起请求，无死网占位）
    result = agent.webtools.fetch_url("https://evil.example.com/x")
    assert not result["ok"] and "domain_not_allowed" in result["error"]


def test_offline_reply_names_env_path(tmp_path):
    from proto.exec.webtools import WebToolkit
    from proto.runtime.agent import ProtoAgent
    agent = ProtoAgent(data_dir=tmp_path, goal_interval_s=10**9,
                       webtools=WebToolkit(fetch=lambda u, a="t":
                                           (_ for _ in ()).throw(OSError("禁网"))))
    reply = agent.chat("你好")
    assert "proto.env" in reply                        # 指出修法，而非只提 BASE_URL
