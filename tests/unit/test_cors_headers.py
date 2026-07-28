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


def test_widget_plane_accepts_any_embedding_origin_without_credentials():
    """§9.6: the widget is embedded on arbitrary customer sites, so its origin can never
    be allow-listed in advance. The data plane must therefore accept any origin — and
    must NOT grant credentials, because a wildcard origin plus credentials would let any
    site drive a cookie-authenticated request."""
    client = TestClient(create_app())

    pre = client.options(
        "/api/v1/chat-widget/bootstrap",
        headers={
            "Origin": "https://some-customer-shop.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert pre.status_code == 200, pre.text
    assert pre.headers.get("access-control-allow-origin") == "*"
    assert "access-control-allow-credentials" not in pre.headers


def test_control_plane_stays_credentialed_and_allow_listed():
    """The split must not loosen the dashboard: the admin session is an httpOnly cookie,
    so its plane keeps credentials AND a strict origin allow-list."""
    client = TestClient(create_app())

    ok = client.options(
        "/api/v1/dashboard/conversations",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:3000"
    assert ok.headers.get("access-control-allow-credentials") == "true"

    bad = client.options(
        "/api/v1/dashboard/conversations",
        headers={
            "Origin": "https://some-customer-shop.example",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert bad.headers.get("access-control-allow-origin") is None
