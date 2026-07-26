"""FastAPI application factory.

Wires CORS for the dashboard origin (credentials:'include'), the httpOnly cookie
session, the frozen error contract, and the v1 routers. The control plane and the
data plane (chat-widget) are mounted as separate routers but co-located in one
ASGI app (Locked Decision §7 #17 — logical separation today).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_error_handlers
from app.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Luciel Backend", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.dashboard_origins,
        allow_credentials=True,  # required for the httpOnly session cookie
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        # content-type + authorization are the app's own headers. sentry-trace + baggage
        # are injected automatically by the frontend's Sentry browser SDK on every fetch
        # (distributed tracing); without them in the allow-list the CORS preflight fails
        # with 'Disallowed CORS headers' (400) and every browser API call is blocked
        # — which silently broke signup/login from the real site.
        allow_headers=["content-type", "authorization", "sentry-trace", "baggage"],
    )

    register_error_handlers(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # v1 routers (mounted as arcs land).
    from app.api.v1.admin.account import router as account_router
    from app.api.v1.admin.connections import router as connections_router
    from app.api.v1.admin.knowledge import router as knowledge_router
    from app.api.v1.admin.luciel import router as luciel_router
    from app.api.v1.admin.usage import router as usage_router
    from app.api.v1.auth import router as auth_router
    from app.api.v1.billing import router as billing_router
    from app.api.v1.chat_widget import router as chat_widget_router
    from app.api.v1.dashboard import router as dashboard_router
    from app.api.v1.webhooks.channels import router as channels_webhook_router
    from app.api.v1.webhooks.stripe import router as stripe_webhook_router

    app.include_router(auth_router)
    app.include_router(luciel_router)
    app.include_router(knowledge_router)
    app.include_router(account_router)
    # Connections layer: one model for every external dependency (§3.8).
    app.include_router(connections_router)
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

    return app


app = create_app()
