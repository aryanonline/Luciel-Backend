"""Permanent guard for the include_router route registry.

A prior dependency bump (fastapi 0.137.x + starlette 1.3) silently dropped routes
that were mounted via ``app.include_router(...)`` — the app booted fine but whole
API surfaces returned 404. This test freezes the full set of registered
(method, path) pairs so that regression can never recur silently: any router that
stops registering (or any route added without updating this list) fails CI.

It checks the framework's own view of the surface two independent ways:
  1. ``app.openapi()`` — what FastAPI believes is registered.
  2. live ASGI routing via ``TestClient`` — every path actually resolves (never 404).
Neither needs Postgres/Redis, so this runs in the unit tier.
"""

from __future__ import annotations

import pytest
from app.main import create_app
from fastapi.testclient import TestClient

# The complete control-plane + data-plane surface mounted in app/main.py via
# include_router, plus the bare /healthz probe. Update this set ONLY with an
# intentional route change (and say so in the PR).
EXPECTED_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/healthz"),
        # auth
        ("POST", "/api/v1/auth/signup"),
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/logout"),
        ("GET", "/api/v1/auth/me"),
        ("POST", "/api/v1/auth/verify-email"),
        ("POST", "/api/v1/auth/resend-verification"),
        ("POST", "/api/v1/auth/forgot-password"),
        ("POST", "/api/v1/auth/reset-password"),
        # admin/luciel
        ("GET", "/api/v1/admin/luciel"),
        ("POST", "/api/v1/admin/luciel"),
        # Decision #8: the owner-facing capability grouping (scheduling is ONE capability).
        ("GET", "/api/v1/admin/luciel/capabilities"),
        ("PUT", "/api/v1/admin/luciel/channels"),
        ("POST", "/api/v1/admin/luciel/delete"),
        ("PUT", "/api/v1/admin/luciel/escalation"),
        # The Admin's opt-in lead auto-prune rule (§3.4.10a) — intentional addition.
        ("PUT", "/api/v1/admin/luciel/lead-retention"),
        ("POST", "/api/v1/admin/luciel/pause"),
        ("PUT", "/api/v1/admin/luciel/personality"),
        ("POST", "/api/v1/admin/luciel/restore"),
        ("POST", "/api/v1/admin/luciel/resume"),
        ("PUT", "/api/v1/admin/luciel/tools"),
        ("POST", "/api/v1/admin/luciel/voice-consent"),
        # admin/knowledge
        ("GET", "/api/v1/admin/knowledge/quota"),
        ("GET", "/api/v1/admin/knowledge/sources"),
        ("POST", "/api/v1/admin/knowledge/sources/paste"),
        ("POST", "/api/v1/admin/knowledge/sources/upload"),
        ("POST", "/api/v1/admin/knowledge/sources/csv"),
        ("DELETE", "/api/v1/admin/knowledge/sources/{source_id}"),
        ("PUT", "/api/v1/admin/knowledge/sources/{source_id}"),
        ("GET", "/api/v1/admin/knowledge/sources/{source_id}/chunks"),
        ("POST", "/api/v1/admin/knowledge/sources/{source_id}/resync"),
        ("POST", "/api/v1/admin/knowledge/sources/{source_id}/replace"),
        # admin/knowledge — live-sync connectors (Architecture §3.2.3, this build).
        # FLAGGED per Build Brief rule 5: new knowledge routes added to this frozen
        # registry as part of the KNOWLEDGE workstream (Arc 11b sync connectors).
        ("GET", "/api/v1/admin/knowledge/sync-connections"),
        ("POST", "/api/v1/admin/knowledge/sync-connections"),
        ("POST", "/api/v1/admin/knowledge/sync-connections/{connection_id}/sync"),
        ("POST", "/api/v1/admin/knowledge/sync-connections/{connection_id}/oauth-callback"),
        # Knowledge SCOPE selection (Decision #9): which folders/pages Luciel may read.
        ("GET", "/api/v1/admin/knowledge/sync-connections/{connection_id}/scope"),
        ("PUT", "/api/v1/admin/knowledge/sync-connections/{connection_id}/scope"),
        ("GET", "/api/v1/admin/knowledge/sync-connections/{connection_id}/scope/candidates"),
        # admin/account
        ("POST", "/api/v1/admin/account/close"),
        ("POST", "/api/v1/admin/account/export"),
        # admin/connections
        ("GET", "/api/v1/admin/connections"),
        ("POST", "/api/v1/admin/connections"),
        ("GET", "/api/v1/admin/connections/email"),
        ("POST", "/api/v1/admin/connections/email"),
        ("DELETE", "/api/v1/admin/connections/{connection_id}"),
        ("POST", "/api/v1/admin/connections/{connection_id}/reconnect"),
        ("POST", "/api/v1/admin/connections/{connection_id}/swap"),
        ("POST", "/api/v1/admin/connections/sms/reverify"),
        # Uniform connection lifecycle + pluggable providers (Decisions #5/#6): every
        # connection type is connect / disconnect / switch-able through these three.
        ("GET", "/api/v1/admin/connections/providers"),
        ("POST", "/api/v1/admin/connections/{connection_id}/disconnect"),
        ("POST", "/api/v1/admin/connections/{connection_id}/switch"),
        # Generic OAuth code-for-token callback (Decision #7): the Meta channels — and
        # every future OAuth provider on a non-knowledge connection type — complete here.
        ("POST", "/api/v1/admin/connections/{connection_id}/oauth-callback"),
        ("POST", "/api/v1/admin/connections/{connection_id}/credentials"),
        # Decision #7: which WhatsApp number / Page this Luciel answers on. Without it a
        # `connected` Meta channel resolves to no tenant and receives nothing.
        ("PUT", "/api/v1/admin/connections/{connection_id}/destination"),
        # admin/usage
        ("GET", "/api/v1/admin/usage/audit-log"),
        ("GET", "/api/v1/admin/usage/overview"),
        # ANALYTICS WORKSTREAM addition (Vision §7 CSV export, Arch §3.9) — flagged
        # per BUILD_BRIEF.md's one allowed cross-file exception.
        ("GET", "/api/v1/admin/usage/export.csv"),
        # dashboard
        ("GET", "/api/v1/dashboard/conversations"),
        ("POST", "/api/v1/dashboard/conversations/{session_id}/hand-back"),
        ("GET", "/api/v1/dashboard/conversations/{session_id}/messages"),
        # Admin-authored reply during takeover (§3.4.12 / §11.5-11.7).
        ("POST", "/api/v1/dashboard/conversations/{session_id}/messages"),
        ("GET", "/api/v1/dashboard/conversations/{session_id}/messages/{message_id}/evidence"),
        ("POST", "/api/v1/dashboard/conversations/{session_id}/messages/{message_id}/flag"),
        ("POST", "/api/v1/dashboard/conversations/{session_id}/take-over"),
        ("GET", "/api/v1/dashboard/escalations"),
        ("GET", "/api/v1/dashboard/leads"),
        # Lead portability (§3.4.10a "exportable", Legal §A7) — intentional addition.
        ("GET", "/api/v1/dashboard/leads/export"),
        ("POST", "/api/v1/dashboard/leads/prune"),
        ("DELETE", "/api/v1/dashboard/leads/{lead_id}"),
        ("POST", "/api/v1/dashboard/leads/{lead_id}/archive"),
        # chat-widget (data plane)
        ("POST", "/api/v1/chat-widget/bootstrap"),
        ("POST", "/api/v1/chat-widget/messages"),
        ("GET", "/api/v1/chat-widget/sessions/{session_id}"),
        # billing
        ("GET", "/api/v1/billing"),
        ("POST", "/api/v1/billing/checkout"),
        ("DELETE", "/api/v1/billing/payment-method"),
        # The providers' registered OAuth redirect target (§3.8) — public by necessity,
        # authorized by the signed single-use state, answers only in 3xx.
        ("GET", "/api/v1/connections/{client}/callback"),
        # The marketing site's contact form (ruling 1) — unauthenticated by nature,
        # gated by hCaptcha + rate limit rather than by a session.
        ("POST", "/api/v1/contact"),
        # webhooks
        ("POST", "/api/v1/webhooks/stripe"),
        ("POST", "/api/v1/webhooks/twilio/sms"),
        ("POST", "/api/v1/webhooks/ses/inbound"),
        ("GET", "/api/v1/webhooks/meta"),
        ("POST", "/api/v1/webhooks/meta"),
    }
)


def _registered_routes() -> set[tuple[str, str]]:
    """All (method, path) pairs FastAPI reports as registered, via the OpenAPI schema.

    Uses the public schema rather than walking ``app.routes`` because newer FastAPI
    nests included routers under internal mount objects rather than flattening them;
    the OpenAPI schema is the stable, public view of the registered surface.
    """
    spec = create_app().openapi()
    return {
        (method.upper(), path)
        for path, operations in spec["paths"].items()
        for method in operations
    }


def test_all_include_router_routes_are_registered() -> None:
    registered = _registered_routes()
    missing = EXPECTED_ROUTES - registered
    unexpected = registered - EXPECTED_ROUTES
    assert not missing, (
        f"include_router routes dropped from the app (regression!): {sorted(missing)}"
    )
    assert not unexpected, (
        "routes present but not in EXPECTED_ROUTES — update this guard intentionally: "
        f"{sorted(unexpected)}"
    )


@pytest.mark.parametrize(("method", "path"), sorted(EXPECTED_ROUTES))
def test_every_route_resolves_not_404(method: str, path: str) -> None:
    """Each registered path must actually route (never 404). Path params get dummy
    values; the route may answer 401/422/405, but a 404 means it is unmounted.

    Redirects are NOT followed: the OAuth callback answers 3xx pointing at the frontend
    origin, which this ASGI client would chase back into the API app and 404 on."""
    client = TestClient(create_app(), follow_redirects=False)
    concrete = path.format(
        source_id="00000000-0000-0000-0000-000000000000",
        connection_id="00000000-0000-0000-0000-000000000000",
        session_id="00000000-0000-0000-0000-000000000000",
        message_id="00000000-0000-0000-0000-000000000000",
        lead_id="00000000-0000-0000-0000-000000000000",
        client="google",
    )
    response = client.request(method, concrete)
    assert response.status_code != 404, f"{method} {concrete} is not routed (404)"
