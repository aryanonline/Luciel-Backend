"""Why the router failed over: one ``llm_failover`` line with a sanitised cause.

A failed primary (Anthropic) call fails over to the fallback (OpenAI), the orchestrator
counts the turn in ``LLMFallbackUsed``, and LlmFallbackAlarm pages at five such turns in
fifteen minutes. Nothing said WHY the primary failed, so a live break of the anthropic
SDK (the 1.15 -> 2.8 upgrade review, PR #19) would have paged with no cause.

Each time a leg's ``ProviderError`` moves a call to another leg, the router now logs ONE
WARNING line: the leg, the provider's routing name, its model pin, the error's ``kind``
(the SDK exception class), the HTTP status, the retryable flag, the failed call's
latency, and the tenant by hash only. The fast leg falling through to the primary logs
the same line (``leg=fast``): it runs the same SDK and no metric records it. The
fallback's own failures log none: they move the call to no other leg. The exception
message is never logged: an SDK message embeds the API's response body, which can echo
the request.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

import pytest
from app.observability.logging import StructuredFormatter
from app.runtime import llm_cap, llm_router
from app.runtime.llm_router import (
    LLMResponse,
    LLMRouter,
    ModelClass,
    ProviderError,
    admin_id_hash,
)
from app.runtime.testing import EchoModel

ROUTER_LOGGER = "app.runtime.llm_router"
SYSTEM = "You are Luciel for Acme Dental."
MSG = [{"role": "user", "content": "hi"}]
# Fast-eligible routing hints (no tools, small context, low complexity).
FAST_HINTS: dict[str, Any] = {"requires_tools": False, "context_tokens": 100, "complexity": 0.1}
# A key-shaped string and a customer's words, as an SDK error message can carry them
# back from the API's response body. Neither may ever reach a log line.
FAKE_KEY = "sk-ant-fake-not-a-real-key-0000"
USER_TEXT = "please book me in for Saturday, my name is Jane Roe"


class _Leg:
    """A provider double with a model pin: raises its scripted errors (one per call),
    then answers."""

    def __init__(self, name: str, model: str, errors: list[ProviderError] | None = None) -> None:
        self.name = name
        self.model_name = model
        self._errors = list(errors or [])
        self.calls = 0

    async def complete(self, system_prompt, messages, *, tools=None) -> LLMResponse:
        self.calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return LLMResponse(
            text="ok", provider=self.name, model_class=ModelClass.primary, confidence=1.0
        )


class _Clock:
    """Stands in for ``time`` inside the router: each reading is 0.25 s after the last,
    so every failed call took exactly 250 ms."""

    def __init__(self) -> None:
        self._now = 100.0

    def monotonic(self) -> float:
        reading = self._now
        self._now += 0.25
        return reading


@pytest.fixture(autouse=True)
def _router_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The router's own ``time`` only: the event loop keeps the real clock.
    monkeypatch.setattr(llm_router, "time", _Clock())
    monkeypatch.setattr(llm_router, "FALLBACK_RETRY_DELAY_RANGE", (0.0, 0.0))
    monkeypatch.setattr(llm_cap, "_store", llm_cap.InMemoryLLMCapStore())


def _failover_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage().startswith("llm_failover ")]


async def test_a_primary_failure_logs_why_once_and_the_fallback_answers(caplog):
    admin = uuid.uuid4()
    overloaded = ProviderError(
        "anthropic_error:OverloadedError", retryable=True, status_code=529, kind="OverloadedError"
    )
    primary = _Leg("anthropic", "claude-sonnet-4-6", [overloaded])
    fallback = _Leg("openai", "gpt-5.4")
    router = LLMRouter(primary=primary, fallback=fallback, admin_id=admin)
    with caplog.at_level(logging.INFO, logger=ROUTER_LOGGER):
        resp = await router.complete(SYSTEM, MSG)

    assert resp.provider == "openai" and resp.used_fallback is True
    assert primary.calls == 1  # still NO retry on the primary (§3.4.3)
    (record,) = _failover_records(caplog)
    assert record.name == ROUTER_LOGGER and record.levelno == logging.WARNING
    assert record.getMessage() == (
        f"llm_failover admin_id_hash={admin_id_hash(admin)} leg=primary provider=anthropic "
        "model=claude-sonnet-4-6 error=OverloadedError status=529 retryable=True latency_ms=250"
    )
    assert str(admin) not in record.getMessage()


async def test_the_line_never_carries_the_message_a_key_or_the_customers_words(caplog):
    leaked = f"Error code: 400 - invalid x-api-key {FAKE_KEY}; you said: {USER_TEXT}"
    error = ProviderError(f"anthropic_error: {leaked}")
    error.__cause__ = ValueError(leaked)  # the SDK exception a provider chains with `from`
    router = LLMRouter(
        primary=_Leg("anthropic", "claude-sonnet-4-6", [error]), fallback=_Leg("openai", "gpt-5.4")
    )
    with caplog.at_level(logging.INFO, logger=ROUTER_LOGGER):
        resp = await router.complete(SYSTEM, [{"role": "user", "content": USER_TEXT}])

    assert resp.used_fallback is True
    (record,) = _failover_records(caplog)
    # Nothing beyond its class is known about this error: that is all the line says.
    assert record.getMessage().endswith(
        "leg=primary provider=anthropic model=claude-sonnet-4-6 error=ProviderError status=- "
        "retryable=False latency_ms=250"
    )
    assert record.exc_info is None  # a traceback would print the message
    # What reaches CloudWatch: every router record, through the JSON formatter.
    rendered = [StructuredFormatter().format(r) for r in caplog.records]
    for text in [r.getMessage() for r in caplog.records] + rendered:
        assert FAKE_KEY not in text
        assert USER_TEXT not in text and "Jane Roe" not in text
        assert SYSTEM not in text
        assert "Error code" not in text


@pytest.mark.parametrize("hints", [{}, FAST_HINTS], ids=["primary", "fast"])
async def test_a_call_that_needs_no_failover_logs_no_line(caplog, hints):
    router = LLMRouter(
        primary=_Leg("anthropic", "claude-sonnet-4-6"),
        fallback=_Leg("openai", "gpt-5.4"),
        fast=_Leg("anthropic_fast", "claude-haiku-4-5"),
    )
    with caplog.at_level(logging.INFO, logger=ROUTER_LOGGER):
        resp = await router.complete(SYSTEM, MSG, **hints)

    assert resp.used_fallback is False
    assert _failover_records(caplog) == []
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert [r for r in warned if r.name == ROUTER_LOGGER] == []


async def test_a_fast_failure_logs_the_same_line_and_the_primary_answers(caplog):
    """The graph-extraction shape: a router built with no tenant, the tenant passed per
    call. The line names that tenant (by hash), not the router's empty binding."""
    admin = uuid.uuid4()
    retired = ProviderError("anthropic_error:NotFoundError", status_code=404, kind="NotFoundError")
    primary = _Leg("anthropic", "claude-sonnet-4-6")
    router = LLMRouter(
        primary=primary,
        fallback=_Leg("openai", "gpt-5.4"),
        fast=_Leg("anthropic_fast", "claude-haiku-4-5", [retired]),
    )
    with caplog.at_level(logging.INFO, logger=ROUTER_LOGGER):
        resp = await router.complete(SYSTEM, MSG, admin_id=admin, **FAST_HINTS)

    # Not a failover by §3.4.3: the primary answers, nothing counts as a fallback turn.
    assert resp.provider == "anthropic" and resp.model_class is ModelClass.primary
    assert resp.used_fallback is False and primary.calls == 1
    (record,) = _failover_records(caplog)
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        f"llm_failover admin_id_hash={admin_id_hash(admin)} leg=fast provider=anthropic_fast "
        "model=claude-haiku-4-5 error=NotFoundError status=404 retryable=False latency_ms=250"
    )


async def test_fast_then_primary_failing_log_one_line_each_in_order(caplog):
    admin = uuid.uuid4()
    tenant = admin_id_hash(admin)
    router = LLMRouter(
        primary=_Leg("anthropic", "claude-sonnet-4-6", [ProviderError(kind="BadRequestError")]),
        fallback=_Leg("openai", "gpt-5.4"),
        fast=_Leg("anthropic_fast", "claude-haiku-4-5", [ProviderError(kind="BadRequestError")]),
    )
    with caplog.at_level(logging.INFO, logger=ROUTER_LOGGER):
        resp = await router.complete(SYSTEM, MSG, admin_id=admin, **FAST_HINTS)

    assert resp.provider == "openai" and resp.used_fallback is True
    # Each line times its own call: the primary's clock starts after the fast leg failed.
    assert [r.getMessage() for r in _failover_records(caplog)] == [
        f"llm_failover admin_id_hash={tenant} leg=fast provider=anthropic_fast "
        "model=claude-haiku-4-5 error=BadRequestError status=- retryable=False latency_ms=250",
        f"llm_failover admin_id_hash={tenant} leg=primary provider=anthropic "
        "model=claude-sonnet-4-6 error=BadRequestError status=- retryable=False latency_ms=250",
    ]


@pytest.mark.parametrize(
    ("fallback_errors", "both_down"),
    [
        ([ProviderError(retryable=True, status_code=429, kind="RateLimitError")], False),
        ([ProviderError(status_code=401, kind="AuthenticationError")], True),
    ],
    ids=["fallback-retried", "both-down"],
)
async def test_the_fallback_leg_logs_no_line_of_its_own(caplog, fallback_errors, both_down):
    """Its 429/529 retry stays on the same leg, and a fallback that fails too has no
    leg left to move to (the orchestrator's llm_unavailable path takes over). Either
    way the primary's cause is already logged."""
    primary_error = ProviderError(retryable=True, status_code=529, kind="OverloadedError")
    router = LLMRouter(
        primary=_Leg("anthropic", "claude-sonnet-4-6", [primary_error]),
        fallback=_Leg("openai", "gpt-5.4", fallback_errors),
    )
    with caplog.at_level(logging.INFO, logger=ROUTER_LOGGER):
        if both_down:
            with pytest.raises(ProviderError):
                await router.complete(SYSTEM, MSG)
        else:
            assert (await router.complete(SYSTEM, MSG)).used_fallback is True

    (record,) = _failover_records(caplog)
    assert " leg=primary provider=anthropic " in record.getMessage()
    assert "error=OverloadedError status=529 retryable=True" in record.getMessage()


async def test_mock_posture_logs_dashes_for_what_it_does_not_know(caplog):
    """EchoModel has no model pin, its error no kind or status, the router no tenant."""
    router = LLMRouter(
        primary=EchoModel(name="echo", raise_provider_error=True),
        fallback=EchoModel(name="echo"),
    )
    with caplog.at_level(logging.INFO, logger=ROUTER_LOGGER):
        await router.complete(SYSTEM, MSG)

    (record,) = _failover_records(caplog)
    assert record.getMessage() == (
        "llm_failover admin_id_hash=- leg=primary provider=echo model=- error=ProviderError "
        "status=- retryable=False latency_ms=250"
    )
