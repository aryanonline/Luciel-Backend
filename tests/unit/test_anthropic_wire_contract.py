"""The Anthropic SDK's wire contract, driven through the REAL client (anthropic 2.x).

Every other Anthropic test swaps the SDK client for an object-level fake, so a green
suite says nothing about what the SDK sends or how it reads a reply. anthropic 2.0
moved every request to API version 2026-10-21 (``temperature``/``top_p``/``top_k``
are a 400 there) and the API key from ``X-Api-Key`` to ``Authorization: Bearer``;
2.5 moved ``OverloadedError`` under ``InternalServerError``. A break in any of these
would not crash a turn: the router would fail every call over to OpenAI, and the break
would show only as ``LLMFallbackUsed`` on live traffic. So these tests run
``AnthropicProvider`` through the real ``AsyncAnthropic`` over an httpx2 mock transport
(no network) and pin what Luciel relies on: the request shape and headers, the usage
fields metering reads, the ``max_tokens`` truncation signal, and the SDK's status ->
exception mapping behind the fallback's one 429/529 retry and the no-retry deadline
(F101). A future SDK major that changes any of it fails here, before a deploy.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import anthropic
import httpx2
import pytest
import pytest_asyncio
from app.config import get_settings
from app.runtime.llm_router import ProviderError
from app.runtime.providers import AnthropicProvider

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
TOOL_TURNS = [
    {"role": "user", "content": "Can I book Friday?"},
    {
        "role": "assistant",
        "content": "Checking.",
        "tool_calls": [
            {"call_id": "toolu_00", "tool_id": "book_appointment", "inputs": {"date": "friday"}}
        ],
    },
    {"role": "tool_result", "call_id": "toolu_00", "content": '{"slots": ["9:00"]}'},
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
        {"role": "user", "content": "Can I book Friday?"},
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
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_00",
                    "content": '{"slots": ["9:00"]}',
                    "is_error": False,
                }
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


@pytest.mark.parametrize(
    ("status", "error_type", "retryable"),
    [
        (429, "rate_limit_error", True),
        (529, "overloaded_error", True),
        (400, "invalid_request_error", False),
        (401, "authentication_error", False),
        (500, "api_error", False),
        (503, "api_error", False),
    ],
)
async def test_a_status_error_maps_through_the_sdk_after_one_attempt(
    provider: AnthropicProvider, wire: _Wire, status: int, error_type: str, retryable: bool
) -> None:
    wire.reply = lambda _request: httpx2.Response(
        status, json={"type": "error", "error": {"type": error_type, "message": "refused"}}
    )
    with pytest.raises(ProviderError) as info:
        await provider.complete("sys", HI)

    assert isinstance(info.value.__cause__, anthropic.APIStatusError)
    assert info.value.status_code == status
    assert info.value.retryable is retryable
    assert len(wire.requests) == 1  # max_retries=0: the router's failover is the retry


@pytest.mark.parametrize("failure", [httpx2.ReadTimeout, httpx2.ConnectError])
async def test_a_timeout_or_lost_connection_fails_over_after_one_attempt(
    provider: AnthropicProvider, wire: _Wire, failure: type[httpx2.TransportError]
) -> None:
    def _fail(request: httpx2.Request) -> httpx2.Response:
        raise failure("simulated", request=request)

    wire.reply = _fail
    with pytest.raises(ProviderError) as info:
        await provider.complete("sys", HI)

    assert isinstance(info.value.__cause__, anthropic.APIConnectionError)
    assert info.value.status_code is None and info.value.retryable is False
    assert len(wire.requests) == 1
