# -*- coding: utf-8 -*-
"""OpenAI 兼容 LLM 客户端（零依赖，urllib 实现）。

任何暴露 POST /chat/completions 的服务都能接：方舟 Ark、OpenAI、
DeepSeek、Ollama、LM Studio……配置走环境变量：

  PROTO_LLM_BASE_URL  如 https://ark.cn-beijing.volcengine.com/api/v3
                      或 http://localhost:11434/v1（Ollama）
  PROTO_LLM_API_KEY   服务密钥（本地服务可留空）
  PROTO_LLM_MODEL     模型名
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from typing import Optional


class LLMError(Exception):
    """LLM 调用失败（网络/HTTP/协议）。调用方应回退到离线路径。"""


class LLMClient(ABC):
    @abstractmethod
    def complete(self, system: str, user: str) -> str:
        """单轮补全。失败抛 LLMError。"""


class OpenAICompatClient(LLMClient):
    def __init__(self, base_url: str, api_key: str = "", model: str = "gpt-4o-mini",
                 timeout_s: int = 60, temperature: float = 0.3):
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._model = model
        self._timeout = timeout_s
        self._temperature = temperature

    @classmethod
    def from_env(cls) -> Optional["OpenAICompatClient"]:
        """未配置 base_url 返回 None——运行时据此进入离线模式。"""
        base = os.environ.get("PROTO_LLM_BASE_URL", "").strip()
        if not base:
            return None
        return cls(
            base_url=base,
            api_key=os.environ.get("PROTO_LLM_API_KEY", "").strip(),
            model=os.environ.get("PROTO_LLM_MODEL", "gpt-4o-mini").strip(),
        )

    def describe(self) -> str:
        return f"{self._model} @ {self._base}"

    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self._model,
            "temperature": self._temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        headers = {"Content-Type": "application/json"}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        req = urllib.request.Request(
            self._base + "/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                ValueError, KeyError) as exc:
            raise LLMError(f"LLM 调用失败: {exc}") from exc
        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM 响应格式异常: {exc}") from exc
