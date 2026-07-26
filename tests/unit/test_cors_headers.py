"""CORS allow-headers regression (real bug: browser signup/login blocked).

The frontend's Sentry browser SDK injects tracing headers (sentry-trace, baggage, and
potentially others) on every fetch. If any requested header isn't allowed, the CORS
preflight fails with 'Disallowed CORS headers' (400) and EVERY browser API call is
blocked -- which silently broke signup and login from the real site (curl worked because
it sent no browser tracing headers). We reflect any requested header (allow_headers=["*"],
credential-safe in Starlette) while keeping Origin strictly allow-listed. This test
freezes both properties so they can't regress.
"""

from __future__ import annotations

from app.main import create_app
from starlette.testclient import TestClient


def test_preflight_allows_arbitrary_headers_but_locks_origin():
    client = TestClient(create_app())

    # An allow-listed origin requesting arbitrary tracing headers must pass preflight.
    ok = client.options(
        "/api/v1/auth/signup",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": (
                "content-type,authorization,sentry-trace,baggage,traceparent,x-anything"
            ),
        },
    )
    assert ok.status_code == 200, ok.text
    reflected = ok.headers.get("access-control-allow-headers", "").lower()
    for h in ("content-type", "authorization", "sentry-trace", "baggage", "traceparent"):
        assert h in reflected, f"header {h!r} not reflected: {reflected!r}"

    # Origin is still strictly allow-listed: an unknown origin must NOT be echoed.
    bad = client.options(
        "/api/v1/auth/signup",
        headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert bad.headers.get("access-control-allow-origin") != "https://evil.example.com"
