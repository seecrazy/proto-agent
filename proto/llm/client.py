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

import hashlib
import json
import os
import re
import threading
import time
from datetime import datetime
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from http.client import HTTPException
from typing import Optional
from urllib.parse import urlsplit

from proto.llm.decision import DECISION_NAME, decision_tool, native_decisions
from proto.llm.decision_stream import read_decision
from proto.llm.errors import (NETWORK_TIMEOUTS, LLMError, ModelPressureWait, ModelRequestStopped,
                              ModelResponseError)


_RESET_AT = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?")
_QUOTA_CODES = frozenset({"1113", "1310", "insufficient_quota", "insufficient_user_quota", "quota_exceeded",
                          "credit_balance_too_low", "billing_hard_limit_reached"})
_QUOTA_WORDS = ("使用上限", "余额不足", "欠费", "额度已用", "insufficient_quota",
                "exceeded your current quota", "billing hard limit")
QUOTA_UNKNOWN_WAIT_S = 6 * 3600


def _provider_error(body):
    """从供应方错误正文里取 (code, message)；正文不是 JSON 时返回空。消息只用于分类，不外传。"""
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return "", ""
    error = data.get("error", data) if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return "", ""
    return str(error.get("code") or "")[:40], str(error.get("message") or "")[:2000]


def quota_reset(code, message):
    """供应方说的是「额度用完」而不是「请求太密」。

    返回重置时刻（秒）；是额度问题但没给时刻返回 None；不是额度问题返回 False。
    智谱 1310「已达到每周/每月使用上限，限额将在 … 重置」与 1113「余额不足」都走 HTTP 429，
    此前一律按瞬时限流处理：每半小时重试一次，还连带同源的其他接入一起冷却
    （2026-11-24 生产实证：两个接入到 11-28 才恢复，却一直在被重试）。"""
    text = str(message or "")
    if str(code) not in _QUOTA_CODES and not any(word in text for word in _QUOTA_WORDS):
        return False
    found = _RESET_AT.search(text)
    if not found:
        return None
    try:
        year, month, day, hour, minute = (int(found.group(index)) for index in range(1, 6))
        second = int(found.group(6) or 0)
        # 供应方按北京时间给出；本机时区不是北京时间时会差几个小时，只影响恢复时点不影响分类
        return datetime(year, month, day, hour, minute, second).timestamp()
    except (ValueError, OverflowError):
        return None


def _http_failure(error):
    body = b""
    try:
        if error.fp is not None:
            body = error.read(65536)          # 最多 64 KiB，只用来分类；读不到就按状态码处理
    except Exception:
        body = b""
    finally:
        if error.fp is not None:
            try:
                error.close()
            except OSError:
                pass
    code, message = _provider_error(body)
    if error.code == 429:
        reset = quota_reset(code, message)
        if reset is not False:
            return ModelPressureWait("model_quota_exhausted", reset or time.time() + QUOTA_UNKNOWN_WAIT_S)
    failure = LLMError(f"LLM 调用失败: HTTP {error.code}")
    failure.provider_code = code or None
    return failure


def pressure_key(base_url, credential):
    origin = urlsplit(base_url)
    port = origin.port or (443 if origin.scheme == "https" else 80)
    identity = json.dumps([origin.scheme, (origin.hostname or "").rstrip("."), port, credential])
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class LLMClient(ABC):
    """M48: 兼容所有客户端的用量回执约定——complete() 后可读
    self.last_usage = {"input_tokens", "output_tokens"}（协议无 usage 时为 None，
    调用方回退字符估算）。"""

    @property
    def last_usage(self):
        local = vars(self).get("_request_usage")
        return getattr(local, "value", None)

    @last_usage.setter
    def last_usage(self, value):
        local = vars(self).get("_request_usage")
        if local is None:
            local = vars(self).setdefault("_request_usage", threading.local())
        local.value = value

    @abstractmethod
    def complete(self, system: str, user: str) -> str:
        """单轮补全。失败抛 LLMError。"""


class _HttpClient(LLMClient):
    @property
    def native_decisions(self):
        return native_decisions(self._style, self._base, self._model, self._execution_mode)

    def _post(self, payload, *, stream=False):
        headers = {"Content-Type": "application/json", **self._credentials()}
        if stream:
            headers["Accept"] = "text/event-stream"
        req = urllib.request.Request(
            self._base + self._path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        return urllib.request.urlopen(req, timeout=self._timeout)

    def complete_decision(self, system, user, *, first_event, cancelled):
        self.last_usage = None
        reason = cancelled()
        if reason:
            raise ModelRequestStopped(reason)
        system += f"\n调用 {DECISION_NAME} 一次提交决策；不要在正文中再输出一份决策。"
        payload = self._payload(system, user)
        payload.update(stream=True, tools=[decision_tool(self._style)])
        zhipu_native = native_decisions(self._style, self._base, self._model, "auto")
        if self._style == "openai":
            payload.update(max_tokens=8192,
                           tool_choice={"type": "function", "function": {"name": DECISION_NAME}})
            if zhipu_native:
                payload["tool_stream"] = True
        else:
            payload["tool_choice"] = {"type": "tool", "name": DECISION_NAME}
        if zhipu_native:
            # GLM decision calls do not benefit from hidden reasoning in the
            # streamed tool envelope, and thinking can consume max_tokens before
            # the small JSON decision is complete.
            payload["thinking"] = {"type": "disabled"}
        started = time.monotonic()
        try:
            with self._post(payload, stream=True) as response:
                decision, usage = read_decision(response, self._style, started=started, timeout=self._timeout,
                                                first_event=first_event, cancelled=cancelled)
        except urllib.error.HTTPError as exc:
            raise _http_failure(exc) from exc
        except urllib.error.URLError as exc:
            code = "model_timeout" if isinstance(exc.reason, NETWORK_TIMEOUTS) else "model_unreachable"
            raise ModelResponseError(code, "headers") from exc
        except NETWORK_TIMEOUTS as exc:
            raise ModelResponseError("model_timeout", "headers") from exc
        except (HTTPException, OSError) as exc:
            raise ModelResponseError("model_unreachable", "headers") from exc
        self.last_usage = usage
        return decision


def _silence_thinking(payload, client):
    """智谱原生端点上普通补全默认关思考。

    思考会把 max_tokens 全部吃光，正文只剩两三个字符（2026-11-24 生产实证：
    独立验收员的回复是 `{"`，于是每一片都「验证器输出不可解析」）。
    PROTO_LLM_THINKING=enabled 可恢复各家默认行为。"""
    if os.environ.get("PROTO_LLM_THINKING", "").strip().lower() in ("enabled", "on", "1", "true"):
        return
    if native_decisions(client._style, client._base, client._model, client._execution_mode):
        payload["thinking"] = {"type": "disabled"}


class OpenAICompatClient(_HttpClient):
    _style = "openai"
    _path = "/chat/completions"

    def __init__(self, base_url: str, api_key: str = "", model: str = "gpt-4o-mini",
                 timeout_s: int = 60, temperature: float = 0.3, *, execution_mode="auto"):
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._model = model
        self._timeout = timeout_s
        self._temperature = temperature
        self._execution_mode = execution_mode

    @classmethod
    def from_env(cls) -> Optional["OpenAICompatClient"]:
        """未配置返回 None——运行时据此进入离线模式。双模型请用 create_llm_stack。"""
        stack = create_llm_stack()
        if stack is None:
            return None
        return stack["main"] if isinstance(stack["main"], cls) else None

    def describe(self) -> str:
        return f"{self._model} @ {self._base}"

    @property
    def pressure_key(self):
        return pressure_key(self._base, self._key)

    def _payload(self, system, user):
        return {
            "model": self._model,
            "temperature": self._temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }

    def _credentials(self):
        return {"Authorization": f"Bearer {self._key}"} if self._key else {}

    def complete(self, system: str, user: str) -> str:
        self.last_usage = None
        payload = self._payload(system, user)
        _silence_thinking(payload, self)
        try:
            with self._post(payload) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise _http_failure(exc) from exc
        except (urllib.error.URLError, ValueError, KeyError, *NETWORK_TIMEOUTS) as exc:
            raise LLMError(f"LLM 调用失败: {exc}") from exc
        usage = body.get("usage") or {}
        self.last_usage = {
            "input_tokens": usage.get("prompt_tokens") or 0,
            "output_tokens": usage.get("completion_tokens") or 0,
        } if usage else None
        try:
            return body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"LLM 响应格式异常: {exc}") from exc


class AnthropicCompatClient(_HttpClient):
    """Anthropic Messages 协议客户端——智谱 Coding Plan 等订阅制端点。

    智谱 Coding Plan：https://open.bigmodel.cn/api/anthropic（GLM-5.3 系列），
    请求走 POST {base}/v1/messages，x-api-key + anthropive-version 头。
    """

    _style = "anthropic"
    _path = "/v1/messages"

    def __init__(self, base_url: str, api_key: str, model: str = "glm-5.3",
                 timeout_s: int = 120, max_tokens: int = 4096, *, execution_mode="auto"):
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._model = model
        self._timeout = timeout_s
        self._max_tokens = max_tokens
        self._execution_mode = execution_mode

    def describe(self) -> str:
        return f"{self._model} @ {self._base} (anthropic)"

    @property
    def pressure_key(self):
        return pressure_key(self._base, self._key)

    def _payload(self, system, user):
        return {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }

    def _credentials(self):
        return {"x-api-key": self._key, "authorization": f"Bearer {self._key}",
                "anthropic-version": "2023-06-01"}

    def complete(self, system: str, user: str) -> str:
        self.last_usage = None
        payload = self._payload(system, user)
        _silence_thinking(payload, self)
        try:
            with self._post(payload) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise _http_failure(exc) from exc
        except (urllib.error.URLError, ValueError, *NETWORK_TIMEOUTS) as exc:
            raise LLMError(f"LLM 调用失败: {exc}") from exc
        usage = body.get("usage") or {}
        self.last_usage = {
            "input_tokens": usage.get("input_tokens") or 0,
            "output_tokens": usage.get("output_tokens") or 0,
            **({"cache_read": usage["cache_read_input_tokens"]}
               if type(usage.get("cache_read_input_tokens")) is int else {}),
        } if usage else None
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
