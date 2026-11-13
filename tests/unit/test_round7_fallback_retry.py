"""One bounded retry on the fallback leg for 429/529-class refusals (round 7 WP-2, item 6).

§3.4.3 is unchanged: no retry on the primary, immediate failover. But the fallback leg
had ZERO retries, so a 529 (overloaded) on Anthropic and a 429 (rate limited) on OpenAI
in the same second declared both providers down and handed the customer the outage
line. The fallback now gets exactly one more attempt, after a short jittered wait kept
inside the provider timeout, and only for the retryable class — anything else still
fails at once.
"""

from __future__ import annotations

import anthropic
import httpx
import openai
import pytest
from app.config import get_settings
from app.runtime import llm_router
from app.runtime.llm_router import (
    FALLBACK_RETRY_DELAY_RANGE,
    RETRYABLE_PROVIDER_STATUSES,
    LLMRouter,
    ProviderError,
    _fallback_retry_delay,
)
from app.runtime.providers import AnthropicProvider, OpenAIProvider
from app.runtime.testing import EchoModel

MSG = [{"role": "user", "content": "hi"}]


class _Scripted(EchoModel):
    """Raises the scripted errors first (one per call), then answers like EchoModel."""

    def __init__(self, name: str, errors: list[ProviderError]) -> None:
        super().__init__(name=name)
        self._errors = list(errors)
        self.calls = 0

    async def complete(self, system_prompt, messages, *, tools=None):  # type: ignore[override]
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return await super().complete(system_prompt, messages, tools=tools)


def _retryable(status: int) -> ProviderError:
    return ProviderError(f"err:{status}", retryable=True, status_code=status)


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_router, "FALLBACK_RETRY_DELAY_RANGE", (0.0, 0.0))


async def test_a_529_on_primary_and_a_429_on_fallback_still_answers() -> None:
    primary = _Scripted("anthropic", [_retryable(529)])
    fallback = _Scripted("openai", [_retryable(429)])
    resp = await LLMRouter(primary=primary, fallback=fallback).complete("sys", MSG)
    assert resp.provider == "openai" and resp.used_fallback is True
    assert primary.calls == 1  # NO retry on the primary (§3.4.3)
    assert fallback.calls == 2  # exactly one retry on the fallback leg


async def test_the_fallback_retry_is_bounded_to_one() -> None:
    primary = _Scripted("anthropic", [_retryable(529)])
    fallback = _Scripted("openai", [_retryable(429), _retryable(429)])
    with pytest.raises(ProviderError):
        await LLMRouter(primary=primary, fallback=fallback).complete("sys", MSG)
    assert fallback.calls == 2


async def test_a_non_retryable_fallback_failure_fails_at_once() -> None:
    primary = _Scripted("anthropic", [_retryable(529)])
    fallback = _Scripted("openai", [ProviderError("openai_error:AuthenticationError")])
    with pytest.raises(ProviderError):
        await LLMRouter(primary=primary, fallback=fallback).complete("sys", MSG)
    assert fallback.calls == 1


async def test_a_healthy_primary_never_touches_the_fallback() -> None:
    primary = _Scripted("anthropic", [])
    fallback = _Scripted("openai", [_retryable(429)])
    resp = await LLMRouter(primary=primary, fallback=fallback).complete("sys", MSG)
    assert resp.provider == "anthropic" and fallback.calls == 0


def test_the_retry_delay_is_short_jittered_and_inside_the_timeout(monkeypatch) -> None:
    monkeypatch.setattr(llm_router, "FALLBACK_RETRY_DELAY_RANGE", FALLBACK_RETRY_DELAY_RANGE)
    low, high = FALLBACK_RETRY_DELAY_RANGE
    ceiling = float(get_settings().llm_timeout_seconds) / 4
    for _ in range(50):
        d = _fallback_retry_delay()
        assert 0.0 <= d <= min(high, ceiling)
        assert d >= min(low, ceiling)
    # A tiny timeout binds the wait: the retry can never stretch a turn past it.
    monkeypatch.setattr(llm_router, "FALLBACK_RETRY_DELAY_RANGE", (10.0, 10.0))
    assert _fallback_retry_delay() <= ceiling


# --- the providers say which failures are retryable --------------------------------- #
def _anthropic_status_error(status: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request)
    return anthropic.APIStatusError("refused", response=response, body=None)


def _openai_status_error(status: int) -> openai.APIStatusError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return openai.APIStatusError("refused", response=response, body=None)


class _RaisingAnthropic:
    def __init__(self, error: Exception) -> None:
        class _Messages:
            async def create(self_inner, **_kw: object) -> object:
                raise error

        self.messages = _Messages()


class _RaisingOpenAI:
    def __init__(self, error: Exception) -> None:
        class _Completions:
            async def create(self_inner, **_kw: object) -> object:
                raise error

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


@pytest.mark.parametrize("status", sorted(RETRYABLE_PROVIDER_STATUSES))
async def test_anthropic_429_and_529_are_retryable(status: int) -> None:
    p = AnthropicProvider(api_key="fake-anthropic-key", model="model-x")
    p._client = _RaisingAnthropic(_anthropic_status_error(status))
    with pytest.raises(ProviderError) as info:
        await p.complete("sys", MSG)
    assert info.value.retryable is True and info.value.status_code == status


@pytest.mark.parametrize("status", [400, 401, 403, 500, 503])
async def test_other_anthropic_statuses_are_not_retryable(status: int) -> None:
    p = AnthropicProvider(api_key="fake-anthropic-key", model="model-x")
    p._client = _RaisingAnthropic(_anthropic_status_error(status))
    with pytest.raises(ProviderError) as info:
        await p.complete("sys", MSG)
    assert info.value.retryable is False and info.value.status_code == status


async def test_openai_429_is_retryable_and_a_connection_error_is_not() -> None:
    p = OpenAIProvider(api_key="fake-openai-key", model="model-y")
    p._client = _RaisingOpenAI(_openai_status_error(429))
    with pytest.raises(ProviderError) as info:
        await p.complete("sys", MSG)
    assert info.value.retryable is True and info.value.status_code == 429
    p._client = _RaisingOpenAI(openai.APIError("boom", request=None, body=None))  # type: ignore[arg-type]
    with pytest.raises(ProviderError) as info:
        await p.complete("sys", MSG)
    assert info.value.retryable is False and info.value.status_code is None
