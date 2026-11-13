"""BYO-webhook sandbox security envelope (Architecture §3.8.6)."""

from __future__ import annotations

import pytest
from app.connections.sandbox import (
    CIRCUIT_FAILURE_THRESHOLD,
    CircuitBreaker,
    WebhookExecutor,
    is_egress_allowed,
    run_byo_webhook,
)
from app.tools.contract import Outcome


def test_egress_blocks_non_allowlisted_host():
    assert is_egress_allowed("https://evil.com/hook", frozenset({"hooks.acme.com"})) is False


def test_egress_blocks_private_and_metadata_ranges():
    # localhost resolves to 127.0.0.1 (loopback) — blocked even if allowlisted.
    assert is_egress_allowed("http://localhost/x", frozenset({"localhost"})) is False
    # A non-http scheme is rejected.
    assert is_egress_allowed("file:///etc/passwd", frozenset({""})) is False


def test_egress_allows_public_allowlisted_host():
    # example.com resolves to a public IP and is allowlisted.
    assert is_egress_allowed("https://example.com/hook", frozenset({"example.com"})) is True


async def _responder_factory(status, body, raise_exc=None):
    async def _r(url, payload, auth_header, timeout):
        if raise_exc is not None:
            raise raise_exc
        return status, body

    return _r


@pytest.mark.asyncio
async def test_ok_result_on_2xx_with_valid_schema():
    breaker = CircuitBreaker()
    ex = WebhookExecutor(responder=await _responder_factory(200, {"status": "done", "ref": "r1"}))
    res = await run_byo_webhook(
        url="https://example.com/hook",
        payload={"x": 1},
        auth_header="Bearer t",
        allowlist=frozenset({"example.com"}),
        breaker=breaker,
        executor=ex,
        now=0.0,
        response_required_fields=frozenset({"status"}),
    )
    assert res.outcome is Outcome.ok and res.payload["ref"] == "r1"


@pytest.mark.asyncio
async def test_timeout_maps_to_failed_and_records_failure():
    breaker = CircuitBreaker()
    ex = WebhookExecutor(responder=await _responder_factory(0, {}, raise_exc=TimeoutError()))
    res = await run_byo_webhook(
        url="https://example.com/hook",
        payload={},
        auth_header=None,
        allowlist=frozenset({"example.com"}),
        breaker=breaker,
        executor=ex,
        now=0.0,
    )
    assert res.outcome is Outcome.failed and res.reason == "timeout"
    assert breaker.consecutive_failures == 1


@pytest.mark.asyncio
async def test_schema_invalid_response_is_failed():
    breaker = CircuitBreaker()
    ex = WebhookExecutor(responder=await _responder_factory(200, {"unexpected": 1}))
    res = await run_byo_webhook(
        url="https://example.com/hook",
        payload={},
        auth_header=None,
        allowlist=frozenset({"example.com"}),
        breaker=breaker,
        executor=ex,
        now=0.0,
        response_required_fields=frozenset({"status"}),
    )
    assert res.outcome is Outcome.failed and res.reason == "schema_invalid"


@pytest.mark.asyncio
async def test_circuit_opens_after_threshold_failures():
    breaker = CircuitBreaker()
    ex = WebhookExecutor(responder=await _responder_factory(500, {}))
    for _ in range(CIRCUIT_FAILURE_THRESHOLD):
        await run_byo_webhook(
            url="https://example.com/hook",
            payload={},
            auth_header=None,
            allowlist=frozenset({"example.com"}),
            breaker=breaker,
            executor=ex,
            now=0.0,
        )
    # Next call short-circuits as circuit_open without calling the endpoint.
    res = await run_byo_webhook(
        url="https://example.com/hook",
        payload={},
        auth_header=None,
        allowlist=frozenset({"example.com"}),
        breaker=breaker,
        executor=ex,
        now=1.0,
    )
    assert res.outcome is Outcome.failed and res.reason == "circuit_open"


@pytest.mark.asyncio
async def test_egress_blocked_short_circuits_before_call():
    breaker = CircuitBreaker()
    called = {"n": 0}

    async def _r(url, payload, auth_header, timeout):
        called["n"] += 1
        return 200, {}

    ex = WebhookExecutor(responder=_r)
    res = await run_byo_webhook(
        url="https://evil.com/hook",
        payload={},
        auth_header=None,
        allowlist=frozenset({"example.com"}),
        breaker=breaker,
        executor=ex,
        now=0.0,
    )
    assert res.outcome is Outcome.failed and res.reason == "egress_blocked"
    assert called["n"] == 0  # never reached the endpoint
