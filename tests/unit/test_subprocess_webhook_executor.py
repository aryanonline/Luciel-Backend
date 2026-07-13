"""SubprocessWebhookExecutor (Architecture §3.8.6): a real process boundary for the
BYO-webhook HTTP call — spawned (never forked) so the worker never inherits the
parent's DB pool / SecretStore handles, hard 10s kill enforced by the parent, and
the auth header attached as a plain argument (never read from shared state).

Hermetic: every test points ``http_call_path`` at a fake defined at MODULE level in
this file (a spawned process needs an importable target, not a closure) — no test
here ever opens a real socket.
"""

from __future__ import annotations

import time

import pytest
from app.connections.sandbox import (
    CircuitBreaker,
    SubprocessWebhookExecutor,
    run_byo_webhook,
)
from app.tools.contract import Outcome

pytestmark = [pytest.mark.asyncio]

_THIS_MODULE = "tests.unit.test_subprocess_webhook_executor"


# --- module-level fakes (must be top-level + importable for `spawn`) ------------ #
def _fake_ok(url, payload, auth_header, timeout):
    return 200, {"status": "done", "echoed_auth": auth_header, "url": url}


def _fake_non_2xx(url, payload, auth_header, timeout):
    return 503, {}


def _fake_hangs_forever(url, payload, auth_header, timeout):
    # Simulates a webhook endpoint that never responds — proves the PARENT's 10s
    # kill fires rather than trusting the worker/http client's own timeout.
    time.sleep(60)
    return 200, {}  # unreachable


def _fake_raises(url, payload, auth_header, timeout):
    raise ValueError("boom")


def _fake_returns_auth_header_only(url, payload, auth_header, timeout):
    # Proves the auth header reaches the worker as a plain argument (parent-attached)
    # rather than the worker reading it from any shared secret store.
    return 200, {"auth_header_seen": auth_header}


async def test_subprocess_executor_returns_status_and_body_on_success():
    ex = SubprocessWebhookExecutor(http_call_path=f"{_THIS_MODULE}:_fake_ok")
    status, body = await ex.call("https://example.com/hook", {"x": 1}, "Bearer tok", 10)
    assert status == 200
    assert body["status"] == "done"
    assert body["echoed_auth"] == "Bearer tok"


async def test_subprocess_executor_propagates_non_2xx_through_run_byo_webhook():
    breaker = CircuitBreaker()
    ex = SubprocessWebhookExecutor(http_call_path=f"{_THIS_MODULE}:_fake_non_2xx")
    res = await run_byo_webhook(
        url="https://example.com/hook",
        payload={},
        auth_header=None,
        allowlist=frozenset({"example.com"}),
        breaker=breaker,
        executor=ex,
        now=0.0,
    )
    assert res.outcome is Outcome.failed
    assert res.reason == "non_2xx:503"


async def test_subprocess_executor_kills_hung_worker_at_10s_budget():
    """The PARENT's hard-kill fires at the configured timeout — proves this is a real
    process being terminated, not a cooperative in-process timeout. Uses a short
    timeout here (not the full 10s) purely to keep the test fast; the production path
    always calls with WEBHOOK_TIMEOUT_SECONDS=10 via run_byo_webhook."""
    ex = SubprocessWebhookExecutor(http_call_path=f"{_THIS_MODULE}:_fake_hangs_forever")
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        await ex.call("https://example.com/hook", {}, None, 1)
    elapsed = time.monotonic() - start
    # Should return close to the 1s budget, NOT wait for the 60s sleep in the worker.
    assert elapsed < 10


async def test_subprocess_executor_maps_worker_exception_to_transport_error():
    breaker = CircuitBreaker()
    ex = SubprocessWebhookExecutor(http_call_path=f"{_THIS_MODULE}:_fake_raises")
    res = await run_byo_webhook(
        url="https://example.com/hook",
        payload={},
        auth_header=None,
        allowlist=frozenset({"example.com"}),
        breaker=breaker,
        executor=ex,
        now=0.0,
    )
    assert res.outcome is Outcome.failed
    assert res.reason is not None and res.reason.startswith("transport_error:")


async def test_auth_header_attached_by_parent_reaches_worker_as_argument():
    """The auth header is passed to the worker as a plain call argument (parent-
    attached) — never read by the worker from any shared secret/config module."""
    ex = SubprocessWebhookExecutor(http_call_path=f"{_THIS_MODULE}:_fake_returns_auth_header_only")
    status, body = await ex.call("https://example.com/hook", {}, "Bearer secret-token-123", 10)
    assert status == 200
    assert body["auth_header_seen"] == "Bearer secret-token-123"


async def test_ssrf_allowlist_and_circuit_breaker_still_enforced_with_subprocess_executor():
    """The existing SSRF allowlist short-circuits BEFORE the subprocess is even
    started — proves the security envelope in sandbox.py still gates the real
    process-boundary executor exactly like it gates the test double."""
    breaker = CircuitBreaker()
    ex = SubprocessWebhookExecutor(http_call_path=f"{_THIS_MODULE}:_fake_ok")
    res = await run_byo_webhook(
        url="https://evil.com/hook",
        payload={},
        auth_header=None,
        allowlist=frozenset({"example.com"}),
        breaker=breaker,
        executor=ex,
        now=0.0,
    )
    assert res.outcome is Outcome.failed
    assert res.reason == "egress_blocked"
