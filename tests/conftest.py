# -*- coding: utf-8 -*-
"""测试套件的公共夹具。"""
import json

import pytest

from proto.tools.webtools import WebToolkit

_SEARCH = json.dumps({"items": [{"full_name": "example/demo", "stargazers_count": 12,
                                 "description": "离线测试用示例仓库"}]})


def _offline_fetch(url, accept="text/html"):
    """没注入 fetch 的 WebToolkit 不出网。

    自主学习会实时请求 GitHub；网络一慢，带 3–5 秒期限的心跳用例就跟着变红
    （2026-11-24 实测一次 tick 6.5 秒）。测试要量的是机制，不是 GitHub 的延迟。
    """
    if "/search/repositories" in url:
        return 200, _SEARCH
    if url.endswith("README.md"):
        return 200, "# Demo\n\n离线测试用 README。\n"
    return 200, "<html><body><p>offline test page</p></body></html>"


@pytest.fixture(autouse=True)
def _no_live_network(monkeypatch):
    monkeypatch.setattr(WebToolkit, "_default_fetch", staticmethod(_offline_fetch))
