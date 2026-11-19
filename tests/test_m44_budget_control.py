# -*- coding: utf-8 -*-
"""M44：额度控制——上限环境变量 + 控制台可观测/可充值。"""
import pytest

from proto.runtime.agent import DEFAULT_POOLS, ProtoAgent, pools_config


class TestPoolsConfig:
    def test_defaults_without_env(self, monkeypatch):
        for k in ("PROTO_BUDGET_GLOBAL_TOKENS", "PROTO_BUDGET_OWNER_TOKENS",
                  "PROTO_BUDGET_SELF_TOKENS", "PROTO_BUDGET_CALLS"):
            monkeypatch.delenv(k, raising=False)
        cfg = pools_config()
        assert cfg == DEFAULT_POOLS

    def test_env_overrides_caps(self, monkeypatch):
        monkeypatch.setenv("PROTO_BUDGET_GLOBAL_TOKENS", "50000")
        monkeypatch.setenv("PROTO_BUDGET_OWNER_TOKENS", "30000")
        monkeypatch.setenv("PROTO_BUDGET_SELF_TOKENS", "2000")
        monkeypatch.setenv("PROTO_BUDGET_CALLS", "300")
        cfg = pools_config()
        assert cfg["global"]["tokens"] == 50000
        assert cfg["owner_tasks"]["tokens"] == 30000
        assert cfg["self_initiated"]["tokens"] == 2000
        assert cfg["global"]["calls"] == 300
        # 未覆盖的维度保持默认
        assert cfg["global"]["wallclock_s"] == DEFAULT_POOLS["global"]["wallclock_s"]

    def test_invalid_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("PROTO_BUDGET_GLOBAL_TOKENS", "not-a-number")
        assert pools_config()["global"]["tokens"] == \
            DEFAULT_POOLS["global"]["tokens"]

    def test_agent_construct_uses_env_caps(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PROTO_BUDGET_SELF_TOKENS", "1234")
        a = ProtoAgent(data_dir=tmp_path / "ag")
        assert a.kernel.pools._config["self_initiated"]["tokens"] == 1234
        assert a.kernel.pools.remaining_all()["self_initiated"]["tokens"] == 1234
        # 默认字典不被污染
        assert DEFAULT_POOLS["self_initiated"]["tokens"] == 8000.0


class TestBudgetViewCaps:
    def test_state_includes_cap_tokens(self, tmp_path):
        """_budget_view 携带 cap_*，前端才能画 余量/上限 占比条。"""
        from proto.web.server import _budget_view
        a = ProtoAgent(data_dir=tmp_path / "ag")
        view = _budget_view(a)
        assert view["global"]["cap_tokens"] == DEFAULT_POOLS["global"]["tokens"]
        assert view["owner_tasks"]["cap_tokens"] == \
            DEFAULT_POOLS["owner_tasks"]["tokens"]
        assert view["global"]["cap_calls"] == DEFAULT_POOLS["global"]["calls"]
        assert "tokens" in view["self_initiated"]

    def test_budget_panel_wired_in_html(self):
        """控制台页面有额度面板与充值按钮（M44 验收：控制台可刷新额度）。"""
        from pathlib import Path
        html = (Path(__file__).resolve().parent.parent
                / "proto" / "web" / "index.html").read_text(encoding="utf-8")
        assert 'id="budget-panel"' in html
        assert "refillBudget" in html
        assert "/api/budget/refill" in html or "budget/refill" in html
        assert "renderBudgetPanel" in html
