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
        """未配置返回 None——运行时据此进入离线模式。双模型请用 create_llm_stack。"""
        stack = create_llm_stack()
        if stack is None:
            return None
        return stack["main"] if isinstance(stack["main"], cls) else None

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


class AnthropicCompatClient(LLMClient):
    """Anthropic Messages 协议客户端——智谱 Coding Plan 等订阅制端点。

    智谱 Coding Plan：https://open.bigmodel.cn/api/anthropic（GLM-5.3 系列），
    请求走 POST {base}/v1/messages，x-api-key + anthropive-version 头。
    """

    def __init__(self, base_url: str, api_key: str, model: str = "glm-5.3",
                 timeout_s: int = 120, max_tokens: int = 1024):
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._model = model
        self._timeout = timeout_s
        self._max_tokens = max_tokens

    def describe(self) -> str:
        return f"{self._model} @ {self._base} (anthropic)"

    def complete(self, system: str, user: str) -> str:
        payload = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        req = urllib.request.Request(
            self._base + "/v1/messages",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-api-key": self._key,
                "authorization": f"Bearer {self._key}",
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                ValueError) as exc:
            raise LLMError(f"LLM 调用失败: {exc}") from exc
        try:
            for block in body["content"]:
                if block.get("type") == "text":
                    return block["text"]
            raise LLMError("响应中没有文本块")
        except (KeyError, TypeError, LLMError) as exc:
            raise LLMError(f"LLM 响应格式异常: {exc}") from exc


# ---- 提供商预设与双模型栈 ----
# 主模型跑对话；快模型（fast）跑高频低风险调用：事实抽取/反思/学习摘要/规划
PRESETS = {
    "zhipu-coding": ("anthropic", "https://open.bigmodel.cn/api/anthropic", "glm-5.3"),
    "zhipu": ("openai", "https://open.bigmodel.cn/api/paas/v4", "glm-5.3"),
    "openai": ("openai", "https://api.openai.com/v1", "gpt-4o-mini"),
    "deepseek": ("openai", "https://api.deepseek.com/v1", "deepseek-chat"),
    "ollama": ("openai", "http://localhost:11434/v1", "qwen3"),
}


def create_llm_stack() -> Optional[dict]:
    """按环境变量组装 {main, fast} 双模型栈；未配置返回 None（离线模式）。

    优先级：PROTO_LLM_BASE_URL（自动识别协议风格）> PROTO_LLM_PROVIDER 预设。
    PROTO_LLM_FAST_MODEL 可选（默认与主模型同款）。
    """
    provider = os.environ.get("PROTO_LLM_PROVIDER", "").strip().lower()
    base = os.environ.get("PROTO_LLM_BASE_URL", "").strip()
    api_key = os.environ.get("PROTO_LLM_API_KEY", "").strip()
    model = os.environ.get("PROTO_LLM_MODEL", "").strip()
    fast_model = os.environ.get("PROTO_LLM_FAST_MODEL", "").strip()
    style_override = os.environ.get("PROTO_LLM_API_STYLE", "").strip().lower()

    if base:
        style = ("anthropic" if (base.rstrip("/").endswith("/anthropic")
                                 or style_override == "anthropic") else "openai")
    elif provider and provider in PRESETS:
        style, base, default_model = PRESETS[provider]
        model = model or default_model
    else:
        return None

    if not model:
        model = "glm-5.3" if style == "anthropic" else "gpt-4o-mini"
    cls = AnthropicCompatClient if style == "anthropic" else OpenAICompatClient
    main = cls(base_url=base, api_key=api_key, model=model)
    fast = (cls(base_url=base, api_key=api_key, model=fast_model)
            if fast_model and fast_model != model else main)
    return {"main": main, "fast": fast}
