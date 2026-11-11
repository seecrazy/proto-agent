# -*- coding: utf-8 -*-
"""M10-a: Web 工具包——白名单/抓取/GitHub 感官（本地假服务，不依赖外网）。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from proto.exec.webtools import WebToolkit


@pytest.fixture()
def fake_github():
    """模拟 api.github.com 与 raw.githubusercontent.com。"""
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append(self.path)
            if self.path.startswith("/search/repositories"):
                payload = {"items": [{"full_name": "owner/awesome-lib",
                                      "stargazers_count": 12345,
                                      "description": "很棒的库"}]}
            elif self.path.startswith("/owner/awesome-lib/HEAD/README.md"):
                payload = "# Awesome Lib\n\n<script>evil()</script>一个库的说明。"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                data = payload.encode()
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            else:
                payload = {}
            data = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    # 白名单校验原始域名；fetch 回调把请求重映射到本地假服务
    toolkit = WebToolkit(
        allowlist=("github.com", "raw.githubusercontent.com", "api.github.com"),
        fetch=lambda url, accept="text/html": _real_fetch_remap(url, base))
    yield toolkit, calls, base
    server.shutdown()


def _real_fetch_remap(url, base):
    import urllib.request
    path = url.split(".com", 1)[1]   # api.github.com 与 raw.githubusercontent.com 通吃
    remapped = base + path
    req = urllib.request.Request(remapped, headers={"User-Agent": "test"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode()


class TestWebToolkit:
    def test_domain_allowlist_enforced(self):
        tk = WebToolkit()
        result = tk.fetch_url("https://evil.example.com/readme")
        assert not result["ok"] and "domain_not_allowed" in result["error"]

    def test_github_search_and_readme(self, fake_github):
        tk, calls, _ = fake_github
        repos = tk.github_search_repos("agent 框架")
        assert repos and repos[0]["full_name"] == "owner/awesome-lib"
        assert repos[0]["stars"] == 12345
        readme = tk.github_readme("owner/awesome-lib")
        assert readme["ok"]
        assert "Awesome Lib" in readme["text"]
        assert "evil()" not in readme["text"]     # script 被剥离

    def test_on_fetch_callback(self, fake_github):
        tk, _, _ = fake_github
        seen = []
        tk.on_fetch = lambda url: seen.append(url)
        tk.github_readme("owner/awesome-lib")
        assert seen and "raw" in seen[0]
