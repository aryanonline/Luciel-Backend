"""The Anthropic SDK's wire contract, driven through the REAL client (anthropic 2.x).

No other test sends a request through the real SDK client: the provider tests swap it
for an object-level fake, and the loop-hygiene test only builds and closes it. So a
green suite says nothing about what the SDK sends, or how it turns a reply or a failure
into Python. anthropic 2.0 moved every request to API version 2026-10-21
(``temperature``/``top_p``/``top_k`` are a 400 there) and the API key from ``X-Api-Key``
to ``Authorization: Bearer``; 2.5 moved ``OverloadedError`` under
``InternalServerError``. A break in any of these would not crash a turn: the router
would fail every primary and fast call over to OpenAI, and live traffic would show only
fallback turns (``LLMFallbackUsed``). So these tests run ``AnthropicProvider`` through
the real ``AsyncAnthropic`` over an httpx2 mock transport (no network) and pin:

- the request: URL, API version, auth headers, exactly the body keys we send, the
  cached system block, two tool results merged into one user message, and the per-call
  deadline at the transport (F101);
- the reply as the SDK builds it: text, tool_use, the four usage fields metering reads
  and the ``max_tokens`` truncation signal. The SDK builds reply models without
  validating them, so this reads back what the mock wrote: it pins the SDK's parsing and
  the field names its models declare, not what the live API returns;
- that every failure (an error status, a timeout, a lost connection) becomes a
  ``ProviderError`` after exactly one attempt, which is what fails the router over, and
  carries the SDK's class name as its ``kind``: the cause the router's ``llm_failover``
  line logs, never the SDK's message, which embeds the response body.

A future SDK major that changes any of it fails here, before a deploy. No offline test
can show that the live API accepts the request.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import AsyncIterator, Callable
from typing import Any

import anthropic
import httpx2
import pytest
import pytest_asyncio
from anthropic.types import Message, TextBlock, ToolUseBlock, Usage
from app.config import get_settings
from app.observability.logging import StructuredFormatter
from app.runtime.llm_router import LLMRouter, ProviderError
from app.runtime.providers import AnthropicProvider
from app.runtime.testing import EchoModel

KEY = "wire-contract-test-key"  # not a real key; the transport is a mock
HI = [{"role": "user", "content": "hi"}]
TOOLS = [
    {
        "tool_id": "book_appointment",
        "description": "Book an appointment.",
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string"}},
            "required": ["date"],
        },
    }
]
# One assistant turn proposing two calls, then both results (the second a failure), in
# the neutral shape the orchestrator builds.
TOOL_TURNS = [
    {"role": "user", "content": "Can I book Friday or Saturday?"},
    {
        "role": "assistant",
        "content": "Checking.",
        "tool_calls": [
            {"call_id": "toolu_00", "tool_id": "book_appointment", "inputs": {"date": "friday"}},
            {"call_id": "toolu_01", "tool_id": "book_appointment", "inputs": {"date": "saturday"}},
        ],
    },
    {
        "role": "tool_result",
        "call_id": "toolu_00",
        "tool_id": "book_appointment",
        "content": '{"slots": ["9:00"]}',
        "is_error": False,
    },
    {
        "role": "tool_result",
        "call_id": "toolu_01",
        "tool_id": "book_appointment",
        "content": '{"outcome": "failed"}',
        "is_error": True,
    },
]


def _message(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "msg_wire",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-6",
        "content": [{"type": "text", "text": "We open at nine."}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 5},
    }
    body.update(overrides)
    return body


def _ok(_request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, json=_message())


class _Wire:
    """Records every request the real SDK sends and answers it with ``reply``."""

    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []
        self.reply: Callable[[httpx2.Request], httpx2.Response] = _ok

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.reply(request)


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch) -> _Wire:
    # The SDK reads ANTHROPIC_BASE_URL and ANTHROPIC_CUSTOM_HEADERS even when the key is
    # passed in, so a shell that routes Claude through a gateway would change the request
    # asserted on below. Upstream clears ambient ANTHROPIC_* in its own tests (2.6.0).
    for name in [key for key in os.environ if key.startswith("ANTHROPIC_")]:
        monkeypatch.delenv(name)
    wire = _Wire()
    real_client = anthropic.AsyncAnthropic

    class _RealClientOverMockTransport(real_client):
        def __init__(self, **kwargs: Any) -> None:
            transport = httpx2.MockTransport(wire.handle)
            super().__init__(**kwargs, http_client=httpx2.AsyncClient(transport=transport))

    # The provider builds its client from this module attribute on first use, so the
    # real constructor receives exactly the arguments production passes.
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _RealClientOverMockTransport)
    return wire


@pytest_asyncio.fixture
async def provider(wire: _Wire) -> AsyncIterator[AnthropicProvider]:
    p = AnthropicProvider(api_key=KEY, model="claude-sonnet-4-6")
    yield p
    if p._client is not None:
        await p._client.close()  # type: ignore[attr-defined]


async def test_request_is_api_2026_10_21_with_a_bearer_key_and_no_sampling_params(
    provider: AnthropicProvider, wire: _Wire
) -> None:
    await provider.complete("You are Luciel.", TOOL_TURNS, tools=TOOLS)

    (request,) = wire.requests
    assert request.method == "POST"
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    # A new API version ships its breaking changes as 400s the failover would hide:
    # moving off 2026-10-21 must be a reviewed change, not a silent SDK bump.
    assert request.headers["anthropic-version"] == "2026-10-21"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    assert "x-api-key" not in request.headers
    # F101: the per-call deadline reaches the transport, not the SDK's ten minutes.
    timeouts = request.extensions["timeout"]
    assert set(timeouts.values()) == {get_settings().llm_timeout_seconds}

    body = json.loads(request.content)
    # Exactly these keys: temperature/top_p/top_k would be a 400 on 2026-10-21.
    assert set(body) == {"model", "max_tokens", "system", "messages", "tools"}
    assert body["model"] == "claude-sonnet-4-6"
    assert body["max_tokens"] == 2048  # a tool-bearing call's floor
    assert body["system"] == [
        {"type": "text", "text": "You are Luciel.", "cache_control": {"type": "ephemeral"}}
    ]
    assert body["messages"] == [
        {"role": "user", "content": "Can I book Friday or Saturday?"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Checking."},
                {
                    "type": "tool_use",
                    "id": "toolu_00",
                    "name": "book_appointment",
                    "input": {"date": "friday"},
                },
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "book_appointment",
                    "input": {"date": "saturday"},
                },
            ],
        },
        # Both results in ONE user message straight after the tool_use turn, as the API
        # documents for parallel tool calls (_to_anthropic_messages merges them).
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_00",
                    "content": '{"slots": ["9:00"]}',
                    "is_error": False,
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_01",
                    "content": '{"outcome": "failed"}',
                    "is_error": True,
                },
            ],
        },
    ]
    assert body["tools"] == [
        {
            "name": "book_appointment",
            "description": "Book an appointment.",
            "input_schema": TOOLS[0]["input_schema"],
        }
    ]


async def test_reply_is_read_through_the_real_message_model(
    provider: AnthropicProvider, wire: _Wire
) -> None:
    wire.reply = lambda _request: httpx2.Response(
        200,
        json=_message(
            content=[
                {"type": "text", "text": "Friday at 9:00 is open.\n<confidence>0.8</confidence>"},
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "book_appointment",
                    "input": {"date": "2026-11-20"},
                },
            ],
            stop_reason="tool_use",
            usage={
                "input_tokens": 120,
                "output_tokens": 40,
                "cache_creation_input_tokens": 900,
                "cache_read_input_tokens": 3000,
            },
        ),
    )
    resp = await provider.complete("sys", HI, tools=TOOLS)

    assert resp.text == "Friday at 9:00 is open." and resp.confidence == 0.8
    assert resp.tool_calls == [
        {"call_id": "toolu_01", "tool_id": "book_appointment", "inputs": {"date": "2026-11-20"}}
    ]
    # Metering and cost read these four: input is the UNCACHED remainder, the cache
    # hit and the cache write are reported apart.
    usage = resp.usage
    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens) == (120, 40)
    assert (usage.cache_read_tokens, usage.cache_write_tokens) == (3000, 900)
    assert resp.truncated is False

    wire.reply = lambda _request: httpx2.Response(200, json=_message(stop_reason="max_tokens"))
    assert (await provider.complete("sys", HI)).truncated is True


def test_the_sdk_models_declare_the_fields_the_provider_reads() -> None:
    """The SDK builds a reply without validating it, so the test above reads back the
    keys the mock wrote. This pins the names on the SDK's own models: a field the SDK
    renames fails here instead of leaving metering to read 0 from a missing attribute."""
    assert {"content", "stop_reason", "usage"} <= set(Message.model_fields)
    assert {"type", "text"} <= set(TextBlock.model_fields)
    assert {"type", "id", "name", "input"} <= set(ToolUseBlock.model_fields)
    assert {
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    } <= set(Usage.model_fields)


@pytest.mark.parametrize(
    ("status", "error_type", "retryable", "kind"),
    [
        (429, "rate_limit_error", True, "RateLimitError"),
        (529, "overloaded_error", True, "OverloadedError"),
        (400, "invalid_request_error", False, "BadRequestError"),
        (401, "authentication_error", False, "AuthenticationError"),
        (500, "api_error", False, "InternalServerError"),
        (503, "api_error", False, "ServiceUnavailableError"),
    ],
)
async def test_a_status_error_maps_through_the_sdk_after_one_attempt(
    provider: AnthropicProvider,
    wire: _Wire,
    status: int,
    error_type: str,
    retryable: bool,
    kind: str,
) -> None:
    wire.reply = lambda _request: httpx2.Response(
        status, json={"type": "error", "error": {"type": error_type, "message": "refused"}}
    )
    with pytest.raises(ProviderError) as info:
        await provider.complete("sys", HI)

    assert isinstance(info.value.__cause__, anthropic.APIStatusError)
    assert info.value.status_code == status
    # The SDK's own class for the status: what the router's llm_failover line logs.
    assert info.value.kind == kind == type(info.value.__cause__).__name__
    assert len(wire.requests) == 1  # max_retries=0: the router's failover is the retry
    # Only the router's fallback leg reads ``retryable`` (one retry for 429/529), and in
    # production that leg is OpenAI: a ProviderError from the Anthropic primary or fast
    # leg fails over at once, whatever the flag says. Pinned in case an Anthropic
    # provider ever sits on the fallback leg.
    assert info.value.retryable is retryable


@pytest.mark.parametrize(
    ("failure", "kind"),
    [(httpx2.ReadTimeout, "APITimeoutError"), (httpx2.ConnectError, "APIConnectionError")],
)
async def test_a_timeout_or_lost_connection_fails_over_after_one_attempt(
    provider: AnthropicProvider, wire: _Wire, failure: type[httpx2.TransportError], kind: str
) -> None:
    def _fail(request: httpx2.Request) -> httpx2.Response:
        raise failure("simulated", request=request)

    wire.reply = _fail
    with pytest.raises(ProviderError) as info:
        await provider.complete("sys", HI)

    assert isinstance(info.value.__cause__, anthropic.APIConnectionError)
    assert info.value.status_code is None and info.value.retryable is False
    assert info.value.kind == kind
    assert len(wire.requests) == 1


async def test_a_failed_primary_logs_the_sdk_cause_and_never_the_response_body(
    provider: AnthropicProvider, wire: _Wire, caplog: pytest.LogCaptureFixture
) -> None:
    """The live break the failover alarm cannot explain: the API refuses what the SDK
    sent. The router's line names it by the SDK's class and status. The SDK's own
    message embeds the response body, which can echo the request (here a key-shaped
    string and the customer's words), and none of it may reach the log."""
    fake_key = "sk-ant-fake-not-a-real-key-0000"
    words = "please book me in for Saturday, my name is Jane Roe"
    body = {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": f"{fake_key}: {words}"},
    }
    wire.reply = lambda _request: httpx2.Response(400, json=body)
    turn = [{"role": "user", "content": words}]
    # The content really is in the SDK's message: the test below is not vacuous.
    with pytest.raises(ProviderError) as info:
        await provider.complete("sys", turn)
    assert fake_key in str(info.value.__cause__) and words in str(info.value.__cause__)

    router = LLMRouter(primary=provider, fallback=EchoModel(name="openai"))
    with caplog.at_level(logging.INFO, logger="app.runtime.llm_router"):
        resp = await router.complete("sys", turn)

    assert resp.used_fallback is True
    (line,) = [r.getMessage() for r in caplog.records if r.getMessage().startswith("llm_failover")]
    assert re.fullmatch(
        r"llm_failover admin_id_hash=- leg=primary provider=anthropic model=claude-sonnet-4-6 "
        r"error=BadRequestError status=400 retryable=False latency_ms=\d+",
        line,
    )
    for record in caplog.records:
        rendered = StructuredFormatter().format(record)
        assert fake_key not in rendered and words not in rendered and "Jane Roe" not in rendered
