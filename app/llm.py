"""LLM Provider：OpenAI 兼容接口 + 离线 Mock。"""

from __future__ import annotations

import asyncio
import random
from typing import Protocol

import httpx

from .config import LLM_RETRIES, LLM_RETRY_BACKOFF_S, LLM_TIMEOUT_S, Settings

_MOCK_POOL = ["嗯", "怎么了", "在", "刚在忙", "……", "说嘛", "你猜", "行吧", "懒得跟你讲"]
# 这些状态码代表"过一会儿可能就好"，值得重试；其余 4xx 直接失败
RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


class LLMError(RuntimeError):
    """模型调用失败（网络、鉴权、格式）。"""


class LLMProvider(Protocol):
    name: str
    model: str

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.9,
        max_tokens: int = 300,
        thinking: bool = False,
    ) -> str: ...


class OpenAICompatProvider:
    """兼容 OpenAI /chat/completions 协议的服务（DeepSeek、豆包、Qwen、Moonshot…）。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = LLM_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = "openai-compat"
        self.model = model
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.9,
        max_tokens: int = 300,
        thinking: bool = False,
    ) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            # DeepSeek 默认开思考模式，而思考模式下 temperature 不生效。
            # 闲聊要的是"随口说"，默认关；结构化任务（抽取/摘要）显式打开。
            "thinking": {"type": "enabled" if thinking else "disabled"},
        }

        last_error = "调用失败"
        for attempt in range(LLM_RETRIES):
            try:
                resp = await self._client.post("chat/completions", json=payload)
            except httpx.HTTPError as exc:
                last_error = f"网络错误：{exc}"
                if attempt + 1 < LLM_RETRIES:
                    await asyncio.sleep(LLM_RETRY_BACKOFF_S * (attempt + 1))
                    continue
                raise LLMError(last_error) from exc

            # 只有"可能过一会儿就好"的错误才重试；4xx 是请求本身有问题，重试没意义
            if resp.status_code in RETRYABLE_STATUS:
                last_error = f"HTTP {resp.status_code}：{resp.text[:200]}"
                if attempt + 1 < LLM_RETRIES:
                    await asyncio.sleep(LLM_RETRY_BACKOFF_S * (attempt + 1))
                    continue
                raise LLMError(last_error)

            if resp.status_code != 200:
                raise LLMError(f"HTTP {resp.status_code}：{resp.text[:300]}")

            try:
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, ValueError) as exc:
                raise LLMError(f"响应格式异常：{resp.text[:300]}") from exc

            return (content or "").strip()

        raise LLMError(last_error)

    async def aclose(self) -> None:
        await self._client.aclose()


class MockProvider:
    """无 API Key 时的降级实现。只用于验证链路，不代表真实体验。"""

    name = "mock"

    def __init__(self, model: str = "mock") -> None:
        self.model = model
        self._rng = random.Random(7)

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.9,
        max_tokens: int = 300,
        thinking: bool = False,
    ) -> str:
        last = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        if last.rstrip().endswith(("？", "?", "吗")):
            reply = "你猜"
        else:
            reply = self._rng.choice(_MOCK_POOL)
        # 偶尔连发两条，验证拆气泡与延迟链路
        if self._rng.random() < 0.35:
            reply = f"{reply}\n{self._rng.choice(['刚在宿舍', '今天课多', '你先说'])}"
        return reply


def build_provider(settings: Settings) -> LLMProvider:
    if settings.mock_mode:
        return MockProvider(model="mock")
    return OpenAICompatProvider(settings.llm_base_url, settings.llm_api_key, settings.llm_model)
