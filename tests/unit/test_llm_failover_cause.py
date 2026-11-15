"""Why the router moved or lost a call: one sanitised cause line per failed leg.

A failed primary (Anthropic) call fails over to the fallback (OpenAI), the orchestrator
counts the turn in ``LLMFallbackUsed``, and LlmFallbackAlarm pages at five such turns in
fifteen minutes. Nothing said WHY the primary failed, so a live break of the anthropic
SDK (the 1.15 -> 2.8 upgrade review, PR #19) would have paged with no cause. When the
fallback failed too, the turn counted in ``LLMUnavailable`` (LlmUnavailableAlarm), and
nothing said why the fallback failed either.

Each time a leg's ``ProviderError`` moves a call to the next leg, the router now logs ONE
WARNING ``llm_failover`` line, before that leg is tried: the leg, the provider's routing
name, its model pin, the error's ``kind`` (the SDK exception class), the HTTP status, the
retryable flag, how long the failed call took, and the tenant by hash only. The fast leg
falling through to the primary logs the same line (``leg=fast``): it runs the same SDK
and no metric records it. When the fallback fails for good, ONE ``llm_fallback_failed``
line carries the same fields and the attempt (2 when its one 429/529 retry failed too).
The exception message is never logged: an SDK message embeds the API's response body,
which can echo the request.

The router's clock is a fake that only a provider call, the cap admission and the backoff
before the fallback's retry move, so every ``latency_ms`` below is exactly the failed
call's own duration. The durations are binary fractions of a second, so the arithmetic is
exact.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import Any

import httpx2
import openai
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
from app.runtime.providers import OpenAIProvider
from app.runtime.testing import EchoModel

ROUTER_LOGGER = "app.runtime.llm_router"
SYSTEM = "You are Luciel for Acme Dental."
MSG = [{"role": "user", "content": "hi"}]
# Fast-eligible routing hints (no tools, small context, low complexity).
FAST_HINTS: dict[str, Any] = {"requires_tools": False, "context_tokens": 100, "complexity": 0.1}
# A key-shaped string and a customer's words, as an SDK error message can carry them
# back from the API's response body. Neither may ever reach a log record.
FAKE_KEY = "sk-ant-fake-not-a-real-key-0000"
USER_TEXT = "please book me in for Saturday, my name is Jane Roe"
# The per-tenant cap admission takes time on the router's clock too: never a call's.
ADMISSION_S = 0.0625
# So does the backoff before the fallback's one 429/529 retry: never the retry's.
BACKOFF_S = 0.5


class _Clock:
    """Stands in for ``time`` inside the router. Reading it never moves it: only a
    provider call, the cap admission and the retry backoff do (``advance``)."""

    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _elapse(seconds: float) -> None:
    """Time passing on the router's (fake) clock, inside a call or an admission."""
    llm_router.time.advance(seconds)


class _SlowAdmission(llm_cap.InMemoryLLMCapStore):
    """The in-memory cap store, with an admission that takes ``ADMISSION_S``."""

    async def aadmit(self, admin_id: str, *, cap: int = llm_cap.LLM_CAP_PER_MINUTE) -> bool:
        _elapse(ADMISSION_S)
        return await super().aadmit(admin_id, cap=cap)


class _Asyncio:
    """Stands in for ``asyncio`` inside the router, which uses it only for the backoff
    before the fallback's retry. The wait passes on the router's clock, so a retry timer
    started before it would show in ``latency_ms``. ``slept`` records each wait."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        _elapse(seconds)
        await asyncio.sleep(0)


class _Leg:
    """A provider double with a model pin. Each call takes ``takes`` seconds on the
    router's clock (a list gives one duration per call, its last repeating), then
    raises its next scripted error, or answers."""

    def __init__(
        self,
        name: str,
        model: str,
        errors: list[ProviderError] | None = None,
        *,
        takes: float | list[float] = 0.0,
    ) -> None:
        self.name = name
        self.model_name = model
        self._errors = list(errors or [])
        self._takes = list(takes) if isinstance(takes, list) else [takes]
        self.calls = 0

    async def complete(self, system_prompt, messages, *, tools=None) -> LLMResponse:
        self.calls += 1
        _elapse(self._takes.pop(0) if len(self._takes) > 1 else self._takes[0])
        if self._errors:
            raise self._errors.pop(0)
        return LLMResponse(
            text="ok", provider=self.name, model_class=ModelClass.primary, confidence=1.0
        )


@pytest.fixture(autouse=True)
def _router_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # The router's own ``time`` and ``asyncio`` only: the event loop keeps the real ones.
    monkeypatch.setattr(llm_router, "time", _Clock())
    monkeypatch.setattr(llm_router, "asyncio", _Asyncio())
    monkeypatch.setattr(llm_router, "FALLBACK_RETRY_DELAY_RANGE", (BACKOFF_S, BACKOFF_S))
    monkeypatch.setattr(llm_cap, "_store", _SlowAdmission())


def _cause_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.getMessage().startswith(("llm_failover ", "llm_fallback_failed "))
    ]


def _cause_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in _cause_records(caplog)]


def _assert_never_logged(records: list[logging.LogRecord], *texts: str) -> None:
    """None of ``texts`` is in a record's message, in anything else the record carries
    (its args, any ``extra=`` field, ``exc_info``, ``stack_info``: a handler that exports
    record attributes would ship them), or in what the JSON formatter writes."""
    for record in records:
        for where in (
            record.getMessage(),
            repr(vars(record)),
            StructuredFormatter().format(record),
        ):
            for text in texts:
                assert text not in where


async def test_a_primary_failure_logs_why_once_and_the_fallback_answers(caplog):
    admin = uuid.uuid4()
    overloaded = ProviderError(
        "anthropic_error:OverloadedError", retryable=True, status_code=529, kind="OverloadedError"
    )
    primary = _Leg("anthropic", "claude-sonnet-4-6", [overloaded], takes=1.5)
    fallback = _Leg("openai", "gpt-5.4", takes=0.625)
    router = LLMRouter(primary=primary, fallback=fallback, admin_id=admin)
    with caplog.at_level(logging.INFO, logger=ROUTER_LOGGER):
        resp = await router.complete(SYSTEM, MSG)

    assert resp.provider == "openai" and resp.used_fallback is True
    assert primary.calls == 1  # still NO retry on the primary (§3.4.3)
    (record,) = _cause_records(caplog)
    assert record.name == ROUTER_LOGGER and record.levelno == logging.WARNING
    # 1500 ms is the primary's own call: not the admission before it, not the fallback.
    assert record.getMessage() == (
        f"llm_failover admin_id_hash={admin_id_hash(admin)} leg=primary provider=anthropic "
        "model=claude-sonnet-4-6 error=OverloadedError status=529 retryable=True latency_ms=1500"
    )
    assert str(admin) not in record.getMessage()


@pytest.mark.parametrize("both_down", [False, True], ids=["fallback-answers", "both-down"])
async def test_no_line_carries_the_message_a_key_or_the_customers_words(caplog, both_down):
    leaked = f"Error code: 400 - invalid x-api-key {FAKE_KEY}; you said: {USER_TEXT}"

    def leaky(prefix: str) -> ProviderError:
        error = ProviderError(f"{prefix}: {leaked}")
        error.__cause__ = ValueError(leaked)  # the SDK exception a provider chains with `from`
        return error

    router = LLMRouter(
        primary=_Leg("anthropic", "claude-sonnet-4-6", [leaky("anthropic_error")], takes=0.25),
        fallback=_Leg("openai", "gpt-5.4", [leaky("openai_error")] if both_down else [], takes=0.5),
    )
    turn = [{"role": "user", "content": USER_TEXT}]
    with caplog.at_level(logging.INFO, logger=ROUTER_LOGGER):
        if both_down:
            with pytest.raises(ProviderError):
                await router.complete(SYSTEM, turn)
        else:
            assert (await router.complete(SYSTEM, turn)).used_fallback is True

    # Nothing beyond its class is known about either error: that is all a line says.
    expected = [
        "llm_failover admin_id_hash=- leg=primary provider=anthropic model=claude-sonnet-4-6 "
        "error=ProviderError status=- retryable=False latency_ms=250"
    ]
    if both_down:
        expected.append(
            "llm_fallback_failed admin_id_hash=- leg=fallback provider=openai model=gpt-5.4 "
            "error=ProviderError status=- retryable=False latency_ms=500 attempt=1"
        )
    assert _cause_lines(caplog) == expected
    assert [r.exc_info for r in _cause_records(caplog)] == [None] * len(expected)
    # Every router record, everything on it, and what reaches CloudWatch.
    _assert_never_logged(caplog.records, FAKE_KEY, USER_TEXT, "Jane Roe", SYSTEM, "Error code")


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
    assert _cause_records(caplog) == []
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert [r for r in warned if r.name == ROUTER_LOGGER] == []


async def test_a_fast_failure_logs_the_same_line_and_the_primary_answers(caplog):
    """The graph-extraction shape: a router built with no tenant, the tenant passed per
    call and admitted first (which takes time of its own). The line names that tenant,
    by hash, and times the fast call alone."""
    admin = uuid.uuid4()
    retired = ProviderError("anthropic_error:NotFoundError", status_code=404, kind="NotFoundError")
    primary = _Leg("anthropic", "claude-sonnet-4-6", takes=1.5)
    router = LLMRouter(
        primary=primary,
        fallback=_Leg("openai", "gpt-5.4"),
        fast=_Leg("anthropic_fast", "claude-haiku-4-5", [retired], takes=0.375),
    )
    with caplog.at_level(logging.INFO, logger=ROUTER_LOGGER):
        resp = await router.complete(SYSTEM, MSG, admin_id=admin, **FAST_HINTS)

    # Not a failover by §3.4.3: the primary answers, nothing counts as a fallback turn.
    assert resp.provider == "anthropic" and resp.model_class is ModelClass.primary
    assert resp.used_fallback is False and primary.calls == 1
    (record,) = _cause_records(caplog)
    assert record.levelno == logging.WARNING
    assert record.getMessage() == (
        f"llm_failover admin_id_hash={admin_id_hash(admin)} leg=fast provider=anthropic_fast "
        "model=claude-haiku-4-5 error=NotFoundError status=404 retryable=False latency_ms=375"
    )


async def test_fast_then_primary_failing_log_one_line_each_in_order(caplog):
    admin = uuid.uuid4()
    tenant = admin_id_hash(admin)
    router = LLMRouter(
        primary=_Leg(
            "anthropic", "claude-sonnet-4-6", [ProviderError(kind="BadRequestError")], takes=1.5
        ),
        fallback=_Leg("openai", "gpt-5.4", takes=0.625),
        fast=_Leg(
            "anthropic_fast",
            "claude-haiku-4-5",
            [ProviderError(kind="BadRequestError")],
            takes=0.375,
        ),
    )
    with caplog.at_level(logging.INFO, logger=ROUTER_LOGGER):
        resp = await router.complete(SYSTEM, MSG, admin_id=admin, **FAST_HINTS)

    assert resp.provider == "openai" and resp.used_fallback is True
    # Each line times its own call: the primary's clock starts after the fast leg failed.
    assert _cause_lines(caplog) == [
        f"llm_failover admin_id_hash={tenant} leg=fast provider=anthropic_fast "
        "model=claude-haiku-4-5 error=BadRequestError status=- retryable=False latency_ms=375",
        f"llm_failover admin_id_hash={tenant} leg=primary provider=anthropic "
        "model=claude-sonnet-4-6 error=BadRequestError status=- retryable=False latency_ms=1500",
    ]


@pytest.mark.parametrize(
    ("fallback_errors", "takes", "fallback_line"),
    [
        # Its one 429/529 retry answers: the call was served, nothing more to say.
        (
            [ProviderError(retryable=True, status_code=429, kind="RateLimitError")],
            [0.25, 0.5],
            None,
        ),
        # Anything else ends the leg at once: attempt 1, timed alone.
        (
            [ProviderError(status_code=401, kind="AuthenticationError")],
            [0.625],
            "error=AuthenticationError status=401 retryable=False latency_ms=625 attempt=1",
        ),
        # The retry fails too: the line names the error that ended it and times the retry
        # alone, not the backoff before it.
        (
            [
                ProviderError(retryable=True, status_code=429, kind="RateLimitError"),
                ProviderError(status_code=500, kind="InternalServerError"),
            ],
            [0.25, 0.875],
            "error=InternalServerError status=500 retryable=False latency_ms=875 attempt=2",
        ),
    ],
    ids=["retried-and-answered", "both-down", "both-down-after-the-retry"],
)
async def test_the_fallback_logs_a_line_only_when_it_fails_for_good(
    caplog, fallback_errors, takes, fallback_line
):
    """Nothing is left to move the call to, so this is the cause LlmUnavailableAlarm
    pages without; the primary's line is already on record before it."""
    admin = uuid.uuid4()
    tenant = admin_id_hash(admin)
    overloaded = ProviderError(retryable=True, status_code=529, kind="OverloadedError")
    router = LLMRouter(
        primary=_Leg("anthropic", "claude-sonnet-4-6", [overloaded], takes=1.5),
        fallback=_Leg("openai", "gpt-5.4", fallback_errors, takes=takes),
    )
    with caplog.at_level(logging.INFO, logger=ROUTER_LOGGER):
        if fallback_line is None:
            assert (await router.complete(SYSTEM, MSG, admin_id=admin)).used_fallback is True
        else:
            with pytest.raises(ProviderError) as info:
                await router.complete(SYSTEM, MSG, admin_id=admin)
            assert info.value is fallback_errors[-1]  # the fallback's own error goes up

    # A retryable first error waited out the backoff on the router's clock, once.
    assert llm_router.asyncio.slept == ([BACKOFF_S] if fallback_errors[0].retryable else [])
    expected = [
        f"llm_failover admin_id_hash={tenant} leg=primary provider=anthropic "
        "model=claude-sonnet-4-6 error=OverloadedError status=529 retryable=True latency_ms=1500"
    ]
    if fallback_line is not None:
        expected.append(
            f"llm_fallback_failed admin_id_hash={tenant} leg=fallback provider=openai "
            f"model=gpt-5.4 {fallback_line}"
        )
    assert _cause_lines(caplog) == expected
    assert {r.levelno for r in _cause_records(caplog)} == {logging.WARNING}


class _Witness(_Leg):
    """A fallback that notes, as it is called, the cause lines already logged, then
    fails in a way the router does not handle: it hangs until the caller gives up, or
    raises something other than a ProviderError."""

    def __init__(self, caplog: pytest.LogCaptureFixture, *, hangs: bool) -> None:
        super().__init__("openai", "gpt-5.4")
        self._caplog = caplog
        self._hangs = hangs
        self.saw: list[str] | None = None

    async def complete(self, system_prompt, messages, *, tools=None) -> LLMResponse:
        self.saw = _cause_lines(self._caplog)
        if self._hangs:
            await asyncio.Event().wait()
        raise RuntimeError("the fallback SDK changed shape")


@pytest.mark.parametrize("hangs", [False, True], ids=["raises", "hangs"])
async def test_the_primary_cause_is_on_record_before_the_fallback_is_tried(caplog, hangs):
    overloaded = ProviderError(retryable=True, status_code=529, kind="OverloadedError")
    fallback = _Witness(caplog, hangs=hangs)
    router = LLMRouter(
        primary=_Leg("anthropic", "claude-sonnet-4-6", [overloaded], takes=1.5), fallback=fallback
    )
    primary_line = (
        "llm_failover admin_id_hash=- leg=primary provider=anthropic model=claude-sonnet-4-6 "
        "error=OverloadedError status=529 retryable=True latency_ms=1500"
    )
    with caplog.at_level(logging.INFO, logger=ROUTER_LOGGER):
        if hangs:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(router.complete(SYSTEM, MSG), timeout=0.05)
        else:
            with pytest.raises(RuntimeError):
                await router.complete(SYSTEM, MSG)

    assert fallback.saw == [primary_line]  # written before the fallback was called
    assert _cause_lines(caplog) == [primary_line]


@pytest.mark.parametrize("both_down", [False, True], ids=["fallback-answers", "both-down"])
async def test_mock_posture_logs_dashes_for_what_it_does_not_know(caplog, both_down):
    """EchoModel has no model pin, its error no kind or status, the router no tenant,
    and an echo call takes no time."""
    router = LLMRouter(
        primary=EchoModel(name="echo", raise_provider_error=True),
        fallback=EchoModel(name="echo", raise_provider_error=both_down),
    )
    with caplog.at_level(logging.INFO, logger=ROUTER_LOGGER):
        if both_down:
            with pytest.raises(ProviderError):
                await router.complete(SYSTEM, MSG)
        else:
            await router.complete(SYSTEM, MSG)

    expected = [
        "llm_failover admin_id_hash=- leg=primary provider=echo model=- error=ProviderError "
        "status=- retryable=False latency_ms=0"
    ]
    if both_down:
        expected.append(
            "llm_fallback_failed admin_id_hash=- leg=fallback provider=echo model=- "
            "error=ProviderError status=- retryable=False latency_ms=0 attempt=1"
        )
    assert _cause_lines(caplog) == expected


# --- the fallback through the REAL OpenAI SDK, over an httpx2 mock transport --------- #
class _OpenAIWire:
    """Answers every request the real OpenAI SDK sends with ``status`` and an error body
    carrying a key-shaped string and the customer's words. Each request takes 0.25 s on
    the router's clock."""

    def __init__(self) -> None:
        self.status = 401
        self.requests: list[httpx2.Request] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        _elapse(0.25)
        body = {
            "error": {
                "message": f"Incorrect API key provided: {FAKE_KEY}. You said: {USER_TEXT}",
                "type": "invalid_request_error",
                "param": None,
                "code": None,
            }
        }
        return httpx2.Response(self.status, json=body)


@pytest.fixture
def openai_wire(monkeypatch: pytest.MonkeyPatch) -> _OpenAIWire:
    # The SDK reads OPENAI_BASE_URL, OPENAI_ORG_ID and more even when the key is passed
    # in: an ambient one would change where the request goes.
    for name in [key for key in os.environ if key.startswith("OPENAI_")]:
        monkeypatch.delenv(name)
    wire = _OpenAIWire()
    real_client = openai.AsyncOpenAI

    class _RealClientOverMockTransport(real_client):
        def __init__(self, **kwargs: Any) -> None:
            transport = httpx2.MockTransport(wire.handle)
            super().__init__(**kwargs, http_client=httpx2.AsyncClient(transport=transport))

    # The provider builds its client from this module attribute on first use, so the
    # real constructor receives exactly the arguments production passes.
    monkeypatch.setattr(openai, "AsyncOpenAI", _RealClientOverMockTransport)
    return wire


@pytest.mark.parametrize(
    ("status", "requests", "fallback_line"),
    [
        (401, 1, "error=AuthenticationError status=401 retryable=False latency_ms=250 attempt=1"),
        (429, 2, "error=RateLimitError status=429 retryable=True latency_ms=250 attempt=2"),
    ],
    ids=["revoked-key", "rate-limited-after-the-retry"],
)
async def test_a_failed_fallback_logs_the_sdk_cause_and_never_the_response_body(
    caplog, openai_wire, status, requests, fallback_line
):
    """Both providers down, the fallback a real OpenAI client: a revoked key or an
    exhausted quota, the causes LlmUnavailableAlarm names. The SDK's message carries the
    response body's error message, here a key-shaped string and the customer's words.
    The line names the SDK's class, the status and the attempt, and none of the body."""
    openai_wire.status = status
    fallback = OpenAIProvider(api_key="unit-test-key", model="gpt-5.4")
    turn = [{"role": "user", "content": USER_TEXT}]
    try:
        # The content really is in the SDK's message: the checks below are not vacuous.
        with pytest.raises(ProviderError) as direct:
            await fallback.complete(SYSTEM, turn)
        assert FAKE_KEY in str(direct.value.__cause__)
        assert USER_TEXT in str(direct.value.__cause__)
        openai_wire.requests.clear()

        router = LLMRouter(
            primary=EchoModel(name="anthropic", raise_provider_error=True), fallback=fallback
        )
        with caplog.at_level(logging.INFO, logger=ROUTER_LOGGER):
            with pytest.raises(ProviderError):
                await router.complete(SYSTEM, turn)
    finally:
        if fallback._client is not None:
            await fallback._client.close()

    assert len(openai_wire.requests) == requests  # the 429 got its one retry, no more
    # The retry waited out the backoff on the router's clock first: 250 is its own time.
    assert llm_router.asyncio.slept == [BACKOFF_S] * (requests - 1)
    assert _cause_lines(caplog) == [
        "llm_failover admin_id_hash=- leg=primary provider=anthropic model=- "
        "error=ProviderError status=- retryable=False latency_ms=0",
        f"llm_fallback_failed admin_id_hash=- leg=fallback provider=openai model=gpt-5.4 "
        f"{fallback_line}",
    ]
    _assert_never_logged(caplog.records, FAKE_KEY, USER_TEXT, "Jane Roe")
