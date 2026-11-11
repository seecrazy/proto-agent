# -*- coding: utf-8 -*-
"""M10: Web 感官（零依赖 urllib）——软件专家人设的"眼睛"。

安全模型：
- 域名白名单（默认 GitHub / MDN / HN / StackOverflow / python.org / arxiv），
  白名单外一律拒绝——它只能去"读技术"，不能乱逛；
- 只读 GET，无 Cookie 无登录态，UA 标明身份；
- 抓回的内容属 CONTENT 通道语义：只做摘要素材，绝不进入反馈归因、
  绝不被当作主人指令（学习提示词显式隔离，防 prompt 注入）；
- 每次 fetch 写 web_fetch 审计。

可注入性：fetch 回调可替换（测试用本地假 GitHub，CI 不依赖外网）。
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

DEFAULT_ALLOWLIST = (
    "github.com", "raw.githubusercontent.com", "api.github.com",
    "developer.mozilla.org", "news.ycombinator.com", "stackoverflow.com",
    "python.org", "pypi.org", "arxiv.org",
)
_UA = "ProtoAgent/0.9 (autonomous-study; +local)"
_MAX_TEXT = 6000

_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\n{3,}")


def _html_to_text(html: str) -> str:
    text = _SCRIPT_STYLE.sub(" ", html)
    text = _TAGS.sub(" ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return _WS.sub("\n\n", text).strip()[:_MAX_TEXT]


class WebToolkit:
    def __init__(self, allowlist: Tuple[str, ...] = DEFAULT_ALLOWLIST,
                 timeout_s: int = 10, fetch: Optional[Callable] = None):
        self._allowlist = tuple(allowlist)
        self._timeout = timeout_s
        self._fetch = fetch or self._default_fetch
        self.on_fetch = None  # 回调(url, ok)——运行时接审计

    # ---- 基础 ----
    def _allowed(self, url: str) -> bool:
        host = urllib.parse.urlparse(url).hostname or ""
        return any(host == d or host.endswith("." + d) for d in self._allowlist)

    def _default_fetch(self, url: str, accept: str = "text/html") -> Tuple[int, str]:
        req = urllib.request.Request(url, headers={
            "User-Agent": _UA, "Accept": accept})
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")

    def fetch_url(self, url: str, accept: str = "text/html") -> Dict:
        """白名单内抓取并抽正文。返回 {ok, status, text|error}。"""
        if not self._allowed(url):
            return {"ok": False, "error": f"domain_not_allowed:{url}"}
        try:
            status, body = self._fetch(url, accept)
            text = body if body.lstrip().startswith("{") else _html_to_text(body)
            return {"ok": True, "status": status, "text": text}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        finally:
            if self.on_fetch:
                self.on_fetch(url)

    # ---- GitHub 感官 ----
    def github_search_repos(self, query: str, limit: int = 3) -> List[Dict]:
        q = urllib.parse.quote(query)
        result = self.fetch_url(
            f"https://api.github.com/search/repositories?q={q}&sort=stars"
            f"&order=desc&per_page={limit}", )
        if not result["ok"]:
            return []
        try:
            items = json.loads(result["text"])["items"]
            return [{"full_name": it["full_name"], "stars": it["stargazers_count"],
                     "description": it.get("description") or ""} for it in items]
        except (ValueError, KeyError, TypeError):
            return []

    def github_readme(self, repo: str) -> Dict:
        return self.fetch_url(
            f"https://raw.githubusercontent.com/{repo}/HEAD/README.md",
            accept="text/plain")
