"""Round 7 WP-6: the model-currency watchdog can actually remediate what it detects.

Each test models a live condition the 26834f1 review found:

  1. the OpenAI successor smoke posted ``max_tokens: 8`` — GPT-5.x rejects that
     parameter (400 unsupported_parameter), and even under the right name 8 tokens is
     spent on reasoning before any answer, so a retired fallback pin could never swap;
  2. ``gpt-5`` and ``gpt-5.4`` derived different families, so a retired ``gpt-5``
     never paired with ``gpt-5.4``;
  3. a swap whose ECS restart failed (or was skipped) logged a WARNING and exited 0 —
     the running fleet kept calling the retired model and nothing paged anyone;
  8. the embedding pin is alert-only, and its alert names the re-embed path that
     actually exists (``app/knowledge/service.py`` via the knowledge_sync sweep).
"""

from __future__ import annotations

from typing import Any

import pytest
from app.runtime import model_currency
from app.runtime.model_currency import (
    SMOKE_MAX_COMPLETION_TOKENS_OPENAI,
    SMOKE_PROMPT,
    run_model_currency_check,
    smoke_request,
)

from tests.unit.live_settings import live_settings
from tests.unit.test_model_currency import (
    ALL_PINS,
    FakeRestarter,
    FakeTransport,
    FakeWriter,
    RecordingSender,
    _openai_listing,
    _Resp,
)

_SETTINGS_KW = {"ANTHROPIC_API_KEY": "sk-ant-fake", "OPENAI_API_KEY": "sk-oai-fake"}

# The smallest completion budget a GPT-5.x reasoning model needs before it writes
# any visible answer in this fake (its hidden reasoning comes first).
_REASONING_BUDGET = 64


@pytest.fixture(autouse=True)
def _no_env_pins(monkeypatch):
    for pin in model_currency.PINS:
        monkeypatch.delenv(pin.env_var, raising=False)
    monkeypatch.delenv("LUCIEL_PLATFORM_SECRET_ARN", raising=False)
    monkeypatch.delenv("LUCIEL_ECS_CLUSTER", raising=False)
    monkeypatch.delenv("LUCIEL_ECS_SERVICES", raising=False)


class Gpt5ChatTransport(FakeTransport):
    """The live GPT-5.x chat-completions contract on the smoke POST: ``max_tokens``
    is a 400 unsupported_parameter, and a ``max_completion_tokens`` ceiling below
    the reasoning budget returns 200 with EMPTY content and finish_reason=length."""

    async def post(self, url: str, *, json: Any = None, headers: Any = None):
        self.posts.append((url, json))
        if "openai" not in url:
            return await super().post(url, json=json, headers=headers)
        if "max_tokens" in (json or {}):
            return _Resp(
                {
                    "error": {
                        "message": "Unsupported parameter: 'max_tokens' is not supported "
                        "with this model. Use 'max_completion_tokens' instead.",
                        "type": "invalid_request_error",
                        "code": "unsupported_parameter",
                    }
                },
                status=400,
            )
        budget = int((json or {}).get("max_completion_tokens") or 0)
        if budget < _REASONING_BUDGET:
            return _Resp({"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
        return _Resp({"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]})


# --- item 1: the smoke turn speaks each provider's own contract ------------------ #


def test_smoke_payload_is_exact_per_provider():
    url, payload, headers = smoke_request("openai", api_key="sk-oai-fake", model_id="gpt-5.6")
    assert url == "https://api.openai.com/v1/chat/completions"
    assert payload == {
        "model": "gpt-5.6",
        "max_completion_tokens": SMOKE_MAX_COMPLETION_TOKENS_OPENAI,
        "messages": [{"role": "user", "content": SMOKE_PROMPT}],
    }
    assert "max_tokens" not in payload  # GPT-5.x answers 400 unsupported_parameter
    assert SMOKE_MAX_COMPLETION_TOKENS_OPENAI >= 4 * _REASONING_BUDGET
    assert headers["Authorization"] == "Bearer sk-oai-fake"

    url, payload, headers = smoke_request(
        "anthropic", api_key="sk-ant-fake", model_id="claude-sonnet-4-8"
    )
    assert url == "https://api.anthropic.com/v1/messages"
    assert payload == {
        "model": "claude-sonnet-4-8",
        "max_tokens": model_currency.SMOKE_MAX_TOKENS_ANTHROPIC,
        "messages": [{"role": "user", "content": SMOKE_PROMPT}],
    }
    assert "max_completion_tokens" not in payload
    assert headers["x-api-key"] == "sk-ant-fake" and headers["anthropic-version"]


async def test_the_default_smoke_posts_the_exact_payload_through_the_transport():
    transport = Gpt5ChatTransport(existing=set())
    failure = await model_currency._default_smoke(
        transport, provider="openai", api_key="sk-oai-fake", model_id="gpt-5.6"
    )
    assert failure is None
    ((url, payload),) = transport.posts
    assert url.endswith("/v1/chat/completions")
    assert payload["max_completion_tokens"] == SMOKE_MAX_COMPLETION_TOKENS_OPENAI
    assert "max_tokens" not in payload

    failure = await model_currency._default_smoke(
        transport, provider="anthropic", api_key="sk-ant-fake", model_id="claude-sonnet-4-8"
    )
    assert failure is None
    assert transport.posts[1][1]["max_tokens"] == model_currency.SMOKE_MAX_TOKENS_ANTHROPIC


async def test_a_200_with_an_empty_completion_fails_the_smoke():
    """An exhausted budget is a 200 with nothing in it — never a passing successor."""

    class _Empty(FakeTransport):
        async def post(self, url: str, *, json: Any = None, headers: Any = None):
            self.posts.append((url, json))
            if "anthropic" in url:
                return _Resp({"content": [], "stop_reason": "max_tokens"})
            return _Resp({"choices": [{"message": {"content": None}, "finish_reason": "length"}]})

    transport = _Empty(existing=set())
    openai_failure = await model_currency._default_smoke(
        transport, provider="openai", api_key="k", model_id="gpt-5.6"
    )
    anthropic_failure = await model_currency._default_smoke(
        transport, provider="anthropic", api_key="k", model_id="claude-sonnet-4-8"
    )
    assert openai_failure == "empty completion (stop=length)"
    assert anthropic_failure == "empty completion (stop=max_tokens)"


async def test_a_retired_openai_fallback_pin_now_swaps_to_its_gpt5_successor():
    """The live condition end to end: gpt-5.4 retires, gpt-5.6 is listed, and the
    successor's smoke must pass under the GPT-5.x contract for the swap to happen."""
    transport = Gpt5ChatTransport(
        existing=set(ALL_PINS) - {"gpt-5.4"},
        openai_listing=_openai_listing(("gpt-5.4", 400), ("gpt-5.6", 900)),
    )
    writer, restarter = FakeWriter(), FakeRestarter()
    result = await run_model_currency_check(
        settings=live_settings(**_SETTINGS_KW),
        transport_factory=lambda: transport,
        email_sender=RecordingSender(),
        secret_writer=writer,
        restarter=restarter,
    )
    assert result["swapped"] == [
        {"setting": "llm_openai_fallback_model", "old": "gpt-5.4", "new": "gpt-5.6"}
    ]
    assert writer.applied[0]["LLM_OPENAI_FALLBACK_MODEL"] == "gpt-5.6"
    assert restarter.restarts == 1
    ((url, payload),) = transport.posts
    assert "openai" in url and "max_tokens" not in payload


async def test_an_openai_successor_that_answers_empty_is_not_swapped():
    """If the provider still spends the whole budget on reasoning, the alert names it
    instead of pinning a model that answers nothing."""

    class _AlwaysEmpty(Gpt5ChatTransport):
        async def post(self, url: str, *, json: Any = None, headers: Any = None):
            self.posts.append((url, json))
            return _Resp({"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})

    transport = _AlwaysEmpty(
        existing=set(ALL_PINS) - {"gpt-5.4"},
        openai_listing=_openai_listing(("gpt-5.4", 400), ("gpt-5.6", 900)),
    )
    sender, writer = RecordingSender(), FakeWriter()
    with pytest.raises(RuntimeError, match="model_currency"):
        await run_model_currency_check(
            settings=live_settings(**_SETTINGS_KW),
            transport_factory=lambda: transport,
            email_sender=sender,
            secret_writer=writer,
            restarter=FakeRestarter(),
        )
    assert writer.applied == []
    ((_to, _subject, body),) = sender.sent
    assert "failed a smoke turn (empty completion (stop=length))" in body
