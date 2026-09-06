"""F101 (2026-09-05 audit): no SDK retries and a hard per-call deadline on both LLM
providers. The router's immediate primary→fallback failover IS the retry policy; a hung
primary must not hold a customer's turn for minutes before it kicks in."""

from __future__ import annotations

import anthropic
import openai
from app.config import get_settings
from app.runtime.providers import AnthropicProvider, OpenAIProvider


class _Recorder:
    kwargs: dict | None = None

    def __init__(self, **kwargs) -> None:
        type(self).kwargs = kwargs


def test_anthropic_client_has_no_sdk_retries_and_a_deadline(monkeypatch):
    class FakeAnthropic(_Recorder):
        pass

    monkeypatch.setattr(anthropic, "AsyncAnthropic", FakeAnthropic)
    provider = AnthropicProvider(api_key="sk-ant-unit", model="claude-sonnet-4-6")
    client = provider._get_client()
    assert isinstance(client, FakeAnthropic)
    assert FakeAnthropic.kwargs == {
        "api_key": "sk-ant-unit",
        "max_retries": 0,
        "timeout": get_settings().llm_timeout_seconds,
    }
    assert provider._get_client() is client  # constructed once


def test_openai_client_has_no_sdk_retries_and_a_deadline(monkeypatch):
    class FakeOpenAI(_Recorder):
        pass

    monkeypatch.setattr(openai, "AsyncOpenAI", FakeOpenAI)
    provider = OpenAIProvider(api_key="sk-oai-unit", model="gpt-5.4")
    provider._get_client()
    assert FakeOpenAI.kwargs == {
        "api_key": "sk-oai-unit",
        "max_retries": 0,
        "timeout": get_settings().llm_timeout_seconds,
    }


def test_the_deadline_is_a_bounded_default_not_the_sdk_ten_minutes():
    # The SDKs default to 600 s with 2 retries; a customer turn is bounded far below that.
    assert 5.0 <= get_settings().llm_timeout_seconds <= 60.0
