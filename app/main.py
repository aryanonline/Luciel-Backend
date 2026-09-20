"""FastAPI application factory.

Wires CORS per PLANE, the httpOnly cookie session, the frozen error contract, and the
v1 routers. The control plane and the data plane (chat-widget) are mounted as separate
routers but co-located in one ASGI app (Locked Decision §7 #17 — logical separation
today).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator, Sequence

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.errors import register_error_handlers
from app.config import get_settings
from app.jobs.inprocess import mailbox_poll_loop, should_run_inprocess_mailbox_poll
from app.knowledge.pgvector import installed_version as _pgvector_installed_version
from app.knowledge.pgvector import too_old_reason as _pgvector_too_old_reason
from app.notifications.escalation_dispatch import drain_background_work
from app.observability.context import admin_id_var, bind_trace, new_trace_id, trace_id_var
from app.observability.logging import install_structured_logging
from app.observability.metrics import install_metrics_logger
from app.runtime.integration_status import (
    database_unreachable_reason,
    degraded_bindings,
    escalation_backlog,
    log_resolved_bindings,
)

# Data plane: the embeddable widget is loaded on ARBITRARY customer websites, so its
# origin can never be allow-listed in advance (§1.2, §9.6).
DATA_PLANE_PREFIXES = ("/api/v1/chat-widget",)
# The ALB routes these prefixes to the CONTROL plane service (infra/lib/compute-stack.ts
# CONTROL_PLANE_PATH_PATTERNS — same list, 2026-09-05 audit F067); everything else,
# including the widget and every webhook, is answered by the narrower data plane.
# tests/unit/test_plane_routing.py refuses a mounted router that is on neither list.
CONTROL_PLANE_PREFIXES = (
    "/api/v1/admin",
    "/api/v1/auth",
    "/api/v1/dashboard",
    "/api/v1/connections",
    "/api/v1/exports",
    "/api/v1/billing",
    "/api/v1/contact",
    "/api/v1/escalation-contact",
)
# Answered by the data plane (the ALB default): public ingress and the platform's
# own health surface.
DATA_PLANE_PUBLIC_PREFIXES = (
    "/api/v1/chat-widget",
    "/api/v1/webhooks",
    "/healthz",
)

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


class TraceContext:
    """Pure-ASGI: one trace id per request, echoed back as X-Request-Id (F035).

    The ALB's X-Amzn-Trace-Id (or a caller's X-Request-Id) is honoured so a
    CloudWatch search on one id spans the edge and the app; otherwise a fresh id
    is minted. The tenant is bound later, by the auth dependency."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        incoming = headers.get(b"x-request-id") or headers.get(b"x-amzn-trace-id")
        trace_id = incoming.decode("latin-1")[:128] if incoming else new_trace_id()
        trace_token = bind_trace(trace_id)
        admin_token = admin_id_var.set("")

        async def send_with_trace(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw = list(message.get("headers") or [])
                raw.append((b"x-request-id", trace_id.encode("latin-1", "ignore")))
                message["headers"] = raw
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace)
        finally:
            trace_id_var.reset(trace_token)
            admin_id_var.reset(admin_token)


_CAP_TOKEN_RE = re.compile(r"(\bcap=)[^&\s\"']+")


class _RedactCapabilityTokens(logging.Filter):
    """Keep webhook capability tokens out of every log line (2026-09-05 audit F072).

    A tenant whose Twilio auth token we do not hold authenticates its webhooks with a
    per-connection ``?cap=`` token in the registered URL (C11). uvicorn's access log
    prints the request path WITH its query string, so every inbound text or call wrote
    a bearer-equivalent secret to CloudWatch. The token stays in the URL Twilio calls;
    it never reaches a log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                _CAP_TOKEN_RE.sub(r"\1REDACTED", a) if isinstance(a, str) else a
                for a in record.args
            )
        if isinstance(record.msg, str) and "cap=" in record.msg:
            record.msg = _CAP_TOKEN_RE.sub(r"\1REDACTED", record.msg)
        return True


def install_log_redaction() -> None:
    """Attach the capability-token filter to the loggers that print request URLs."""
    for name in ("uvicorn.access", "uvicorn.error", "app"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, _RedactCapabilityTokens) for f in logger.filters):
            logger.addFilter(_RedactCapabilityTokens())


@contextlib.asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Background work that belongs to the running process, not a scheduler:
    the data plane's in-process mailbox poll (cost) — started only where the
    environment asks for it, stopped cleanly on shutdown — and, on shutdown, the
    escalation notifications and retry ladders running off customers' turns
    (round 7 WP-2, item 5): a deploy must not drop a hot lead's page on the floor."""
    settings = get_settings()
    stop = asyncio.Event()
    poller: asyncio.Task[int] | None = None
    if should_run_inprocess_mailbox_poll(settings):
        poller = asyncio.create_task(
            mailbox_poll_loop(stop, interval_seconds=settings.inprocess_mailbox_poll_seconds)
        )
        logging.getLogger(__name__).info(
            "inprocess_mailbox_poll_started interval=%ss",
            settings.inprocess_mailbox_poll_seconds,
        )
    try:
        yield
    finally:
        stop.set()
        if poller is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(poller, timeout=10)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(drain_background_work(), timeout=15)


def create_app() -> FastAPI:
    settings = get_settings()
    install_log_redaction()
    install_structured_logging(settings.log_format)
    install_metrics_logger()
    app = FastAPI(title="Luciel Backend", version="0.1.0", lifespan=_lifespan)

    # Outermost: every line logged while serving the request carries its trace id.
    app.add_middleware(TraceContext)
    app.add_middleware(
        PlaneAwareCORS,
        dashboard_origins=settings.dashboard_origins,
        data_plane_prefixes=DATA_PLANE_PREFIXES,
    )

    register_error_handlers(app)

    # State the resolved integration mode once, at boot. A stack that fabricates every
    # send otherwise looks identical to a working one from the outside.
    log_resolved_bindings()

    async def pgvector_too_old_reason() -> str | None:
        from app.db.engine import get_sessionmaker  # noqa: PLC0415 — import cost off boot path

        try:
            async with get_sessionmaker()() as s:
                return _pgvector_too_old_reason(await _pgvector_installed_version(s))
        except Exception as exc:  # noqa: BLE001 — health must never raise
            return f"pgvector_too_old:unreadable:{type(exc).__name__}"

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
        # Escalation notifications nobody has heard yet (audit F079): a number, not a
        # ``degraded`` entry — that list gates deploys on binding truth, this is an
        # operations signal. ``None`` when the database is unreachable.
        backlog = await escalation_backlog() if database_reason is None else None
        if database_reason is None:
            # Round 6 WP-G (F044): retrieval quality depends on pgvector 0.8+'s iterative
            # scans; an older extension is a binding problem this list exists to surface.
            too_old = await pgvector_too_old_reason()
            if too_old:
                degraded.append(too_old)
        return {
            "status": "ok" if database_reason is None else "degraded",
            "integrationMode": settings.integration_mode,
            "degraded": degraded,
            "escalationBacklog": backlog,
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
    from app.api.v1.exports import router as exports_router
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
    # Account-export download door (§3.6.6): token-authorized, opened from email.
    app.include_router(exports_router)

    return app


app = create_app()
