"""CORS allow-headers regression (real bug: browser signup/login blocked).

The frontend's Sentry browser SDK injects `sentry-trace` and `baggage` headers on
every fetch. If they aren't in the API's CORS allow_headers, the preflight fails with
'Disallowed CORS headers' (400) and EVERY browser API call is blocked — which silently
broke signup and login from the real site (curl worked because it sent no Sentry
headers). This test freezes the required allow-list so it can't regress.
"""

from __future__ import annotations

from starlette.middleware.cors import CORSMiddleware

from app.main import create_app


def _cors_allow_headers() -> set[str]:
    app = create_app()
    for m in app.user_middleware:
        if m.cls is CORSMiddleware:
            # allow_headers is normalized to lowercase by Starlette.
            return {h.lower() for h in m.kwargs.get("allow_headers", [])}
    raise AssertionError("CORSMiddleware not installed")


def test_cors_allows_app_and_sentry_headers():
    allowed = _cors_allow_headers()
    for required in ("content-type", "authorization", "sentry-trace", "baggage"):
        assert required in allowed, f"CORS allow_headers missing {required!r}: {allowed}"
