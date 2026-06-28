"""Unit tests for the live LLM providers (Architecture §3.4.3).

Hermetic: the SDK clients are replaced with fakes (no anthropic/openai network, no
keys). We assert (1) provider exceptions map to ProviderError, (2) responses map to
LLMResponse, and (3) primary raising ProviderError drives the router to the fallback.
"""

from __future__ import annotations

import anthropic
import openai
import pytest
from app.runtime.llm_router import LLMResponse, ProviderError
from app.runtime.providers import AnthropicProvider, OpenAIProvider


# --- fakes for the Anthropic SDK -------------------------------------------
class _Block:
    def __init__(self, **kw: object) -> None:
        self.__dict__.update(kw)


class _FakeAnthropicMessages:
    def __init__(self, *, response: object = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error

    async def create(self, **_kw: object) -> object:
        if self._error is not None:
            raise self._error
        return self._response


class _FakeAnthropicClient:
    def __init__(self, *, response: object = None, error: Exception | None = None) -> None:
        self.messages = _FakeAnthropicMessages(response=response, error=error)


# --- fakes for the OpenAI SDK ----------------------------------------------
class _FakeMessage:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _FakeChoice:
    def __init__(self, content: str | None) -> None:
        self.message = _FakeMessage(content)


class _FakeOpenAIResponse:
    def __init__(self, content: str | None) -> None:
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, *, response: object = None, error: Exception | None = None) -> None:
        self._response = response
        self._error = error

    async def create(self, **_kw: object) -> object:
        if self._error is not None:
            raise self._error
        return self._response


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeOpenAIClient:
    def __init__(self, *, response: object = None, error: Exception | None = None) -> None:
        self.chat = _FakeChat(_FakeCompletions(response=response, error=error))


# ---------------------------------------------------------------------------
# AnthropicProvider
# ---------------------------------------------------------------------------
async def test_anthropic_maps_response_text_and_tool_calls() -> None:
    resp = _Block(
        content=[
            _Block(type="text", text="hello "),
            _Block(type="text", text="world"),
            _Block(type="tool_use", name="lookup", input={"q": "x"}),
        ]
    )
    p = AnthropicProvider(api_key="fake-anthropic-key", model="model-x")
    p._client = _FakeAnthropicClient(response=resp)

    out = await p.complete("sys", [{"role": "user", "content": "hi"}])
    assert isinstance(out, LLMResponse)
    assert out.text == "hello world"
    assert out.provider == "anthropic"
    assert out.tool_calls == [{"tool_id": "lookup", "inputs": {"q": "x"}}]


async def test_anthropic_error_maps_to_provider_error() -> None:
    err = anthropic.APIError("boom", request=None, body=None)  # type: ignore[arg-type]
    p = AnthropicProvider(api_key="fake-anthropic-key", model="model-x")
    p._client = _FakeAnthropicClient(error=err)

    with pytest.raises(ProviderError):
        await p.complete("sys", [{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# OpenAIProvider
# ---------------------------------------------------------------------------
async def test_openai_maps_response_text() -> None:
    p = OpenAIProvider(api_key="fake-openai-key", model="model-y")
    p._client = _FakeOpenAIClient(response=_FakeOpenAIResponse("answer"))

    out = await p.complete("sys", [{"role": "user", "content": "hi"}])
    assert out.text == "answer"
    assert out.provider == "openai"


async def test_openai_error_maps_to_provider_error() -> None:
    p = OpenAIProvider(api_key="fake-openai-key", model="model-y")
    p._client = _FakeOpenAIClient(error=openai.APIError("boom", request=None, body=None))  # type: ignore[arg-type]

    with pytest.raises(ProviderError):
        await p.complete("sys", [{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# Router failover: primary (Anthropic) raises → fallback (OpenAI) used.
# ---------------------------------------------------------------------------
async def test_primary_failure_triggers_fallback() -> None:
    from app.runtime.llm_router import LLMRouter

    primary = AnthropicProvider(api_key="fake-anthropic-key", model="m")
    primary._client = _FakeAnthropicClient(
        error=anthropic.APIError("down", request=None, body=None)  # type: ignore[arg-type]
    )
    fallback = OpenAIProvider(api_key="fake-openai-key", model="m")
    fallback._client = _FakeOpenAIClient(response=_FakeOpenAIResponse("from-openai"))

    router = LLMRouter(primary=primary, fallback=fallback)
    out = await router.complete("sys", [{"role": "user", "content": "hi"}])
    assert out.text == "from-openai"
    assert out.used_fallback is True
