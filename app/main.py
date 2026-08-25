"""FastAPI application factory.

Wires CORS per PLANE, the httpOnly cookie session, the frozen error contract, and the
v1 routers. The control plane and the data plane (chat-widget) are mounted as separate
routers but co-located in one ASGI app (Locked Decision §7 #17 — logical separation
today).
"""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.errors import register_error_handlers
from app.config import get_settings
from app.runtime.integration_status import (
    database_unreachable_reason,
    degraded_bindings,
    log_resolved_bindings,
)

# Data plane: the embeddable widget is loaded on ARBITRARY customer websites, so its
# origin can never be allow-listed in advance (§1.2, §9.6).
DATA_PLANE_PREFIXES = ("/api/v1/chat-widget",)

_CORS_METHODS = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]


class PlaneAwareCORS:
    """Route each request to the CORS policy of ITS plane (§0 two-plane split, §9.6).

    One global credentialed allow-list cannot serve both planes: the control plane needs
    ``allow_credentials`` for the httpOnly session cookie (which forbids a wildcard
    origin), while the widget data plane must answer browsers on any embedding domain.
    Applying the dashboard-only allow-list to the widget blocked every real embed.

    The split is safe precisely because the data plane carries NO ambient authority: it
    authenticates by embed key in the request body/query, never a cookie, so it runs with
    ``allow_credentials=False`` and the browser will not attach cookies to it.
    """

    def __init__(
        self, app: ASGIApp, *, dashboard_origins: Sequence[str], data_plane_prefixes: Sequence[str]
    ) -> None:
        self._prefixes = tuple(data_plane_prefixes)
        self._data_plane = CORSMiddleware(
            app,
            allow_origins=["*"],
            allow_credentials=False,  # embed-key auth in the payload, never a cookie
            allow_methods=_CORS_METHODS,
            allow_headers=["*"],
        )
        self._control_plane = CORSMiddleware(
            app,
            allow_origins=list(dashboard_origins),
            allow_credentials=True,  # required for the httpOnly session cookie
            allow_methods=_CORS_METHODS,
            # Reflect ANY requested request-header. The frontend's Sentry browser SDK
            # (and other browser tooling) inject tracing headers (sentry-trace, baggage,
            # and potentially others) on every fetch; enumerating them was whack-a-mole
            # and a single missing one fails the CORS preflight (400 'Disallowed CORS
            # headers'), silently blocking ALL browser API calls (signup/login). With
            # credentials mode, Starlette treats "*" by echoing back the specific
            # requested headers (it does not emit a literal "*"), which is
            # credential-safe. Origin is still strictly allow-listed
            # (dashboard_origins), so this does not broaden origin access.
            allow_headers=["*"],
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and str(scope.get("path", "")).startswith(self._prefixes):
            await self._data_plane(scope, receive, send)
            return
        await self._control_plane(scope, receive, send)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Luciel Backend", version="0.1.0")

    app.add_middleware(
        PlaneAwareCORS,
        dashboard_origins=settings.dashboard_origins,
        data_plane_prefixes=DATA_PLANE_PREFIXES,
    )

    register_error_handlers(app)

    # State the resolved integration mode once, at boot. A stack that fabricates every
    # send otherwise looks identical to a working one from the outside.
    log_resolved_bindings()

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        # Binding NAMES only — never a credential and never whether a given secret is
        # set. ``degraded`` non-empty in live mode means sends are being fabricated.
        degraded = degraded_bindings()
        database_reason = await database_unreachable_reason()
        if database_reason is not None:
            degraded.append(f"database:{database_reason}")

        # Still HTTP 200 with the database down, deliberately. This path is also the
        # ALB target health check: failing it would drain every target at once (the
        # service starts answering 503 instead of degraded) and would let the ECS
        # deployment circuit breaker roll back a perfectly good release because a
        # dependency blipped. Restarting a task has never fixed an unreachable
        # database. So liveness keeps its 200 and the BODY carries the truth.
        return {
            "status": "ok" if database_reason is None else "degraded",
            "integrationMode": settings.integration_mode,
            "degraded": degraded,
        }

    # v1 routers (mounted as arcs land).
    from app.api.v1.admin.account import router as account_router
    from app.api.v1.admin.connections import router as connections_router
    from app.api.v1.admin.knowledge import router as knowledge_router
    from app.api.v1.admin.luciel import router as luciel_router
    from app.api.v1.admin.usage import router as usage_router
    from app.api.v1.auth import router as auth_router
    from app.api.v1.billing import router as billing_router
    from app.api.v1.chat_widget import router as chat_widget_router
    from app.api.v1.connections_callback import router as connections_callback_router
    from app.api.v1.contact import router as contact_router
    from app.api.v1.dashboard import router as dashboard_router
    from app.api.v1.escalation_contact import router as escalation_contact_router
    from app.api.v1.webhooks.channels import router as channels_webhook_router
    from app.api.v1.webhooks.stripe import router as stripe_webhook_router

    app.include_router(auth_router)
    app.include_router(luciel_router)
    app.include_router(knowledge_router)
    app.include_router(account_router)
    # Connections layer: one model for every external dependency (§3.8).
    app.include_router(connections_router)
    # The providers' registered OAuth redirect target: unauthenticated by necessity
    # (a provider-driven browser navigation), authorized by the signed single-use state.
    app.include_router(connections_callback_router)
    # Control plane: dashboard conversations/leads (cookie-authed, §3.4).
    app.include_router(dashboard_router)
    # Data plane: embeddable widget — ZERO admin surface (§1.2, §6.3).
    app.include_router(chat_widget_router)
    # Billing data plane (admin's own account) + usage dashboard (§3.4.1b, §3.9).
    app.include_router(billing_router)
    app.include_router(usage_router)
    # Stripe webhooks: signature-verified before routing (§3.4.1b, HARD CONSTRAINT #4).
    app.include_router(stripe_webhook_router)
    # Inbound channel webhooks: SMS/email/Meta — signature-verified before tenant
    # lookup (§3.1.3), pre-budget admission (§3.1.5), then the agentic loop (§3.4.1).
    app.include_router(channels_webhook_router)
    # Marketing contact form: unauthenticated by necessity, captcha + IP bucket instead
    # of a session (ruling 1).
    app.include_router(contact_router)
    # Escalation-contact confirmation (round 5B item 13): public by necessity —
    # the contact may not be the account owner; the signed single-use token in
    # the confirmation email is the authorization.
    app.include_router(escalation_contact_router)

    return app


app = create_app()
