# -*- coding: utf-8 -*-
"""测试套件的公共夹具。"""
import json
import urllib.parse

import pytest

from proto.tools.webtools import WebToolkit

_REAL_FETCH = WebToolkit._default_fetch
_SEARCH = json.dumps({"items": [{"full_name": "example/demo", "stargazers_count": 12,
                                 "description": "离线测试用示例仓库"}]})


def _offline_fetch(self, url, accept="text/html"):
    """没注入 fetch 的 WebToolkit 不出公网。

    自主学习会实时请求 GitHub；网络一慢，带 3–5 秒期限的心跳用例就跟着变红
    （2026-11-24 实测一次 tick 6.5 秒）。测试要量的是机制，不是 GitHub 的延迟。
    指向本机回环地址的请求（测试自建的假服务）照常真实发出。
    """
    host = urllib.parse.urlparse(url).hostname or ""
    if host in ("127.0.0.1", "localhost", "::1"):
        return _REAL_FETCH(self, url, accept)
    if "/search/repositories" in url:
        return 200, _SEARCH
    if url.endswith("README.md"):
        return 200, "# Demo\n\n离线测试用 README。\n"
    return 200, "<html><body><p>offline test page</p></body></html>"


@pytest.fixture(autouse=True)
def _no_live_internet(monkeypatch):
    monkeypatch.setattr(WebToolkit, "_default_fetch", _offline_fetch)
