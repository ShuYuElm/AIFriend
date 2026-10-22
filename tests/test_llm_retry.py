"""LLM 调用的重试行为。"""

import httpx
import pytest

from app.llm import LLMError, OpenAICompatProvider


def _ok(content="好"):
    return httpx.Response(
        200, json={"choices": [{"message": {"content": content}}]}
    )


def _provider(handler) -> OpenAICompatProvider:
    return OpenAICompatProvider(
        "https://api.example.com", "sk-test", "test-model",
        transport=httpx.MockTransport(handler),
    )


async def test_retries_on_500_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="boom")
        return _ok("回来了")

    provider = _provider(handler)
    assert await provider.chat([{"role": "user", "content": "hi"}]) == "回来了"
    assert calls["n"] == 2
    await provider.aclose()


async def test_does_not_retry_on_400():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    provider = _provider(handler)
    with pytest.raises(LLMError) as exc:
        await provider.chat([{"role": "user", "content": "hi"}])
    assert "400" in str(exc.value)
    assert calls["n"] == 1
    await provider.aclose()


async def test_gives_up_after_retries():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, text="unavailable")

    provider = _provider(handler)
    with pytest.raises(LLMError):
        await provider.chat([{"role": "user", "content": "hi"}])
    assert calls["n"] == 2
    await provider.aclose()


async def test_retries_on_network_error():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("no route")
        return _ok()

    provider = _provider(handler)
    assert await provider.chat([{"role": "user", "content": "hi"}]) == "好"
    await provider.aclose()
