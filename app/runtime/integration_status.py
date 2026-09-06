"""What this process is actually bound to (Architecture §3.4.3 integration-mode seam).

``integration_mode`` defaults to ``mock`` in the application, in CDK and in the deploy
workflow, and every delivery factory degrades to a Null implementation rather than
raising. A deployment that fabricates every send is therefore operationally
indistinguishable from a working one unless the resolved mode and the resolved bindings
are stated out loud. This module is the single place that answers "what is bound?", for
the startup log and for ``/healthz``.

It reports binding NAMES only (``seam:ImplClass``) — never a credential, never a value,
never whether a specific secret is present (§3.8.3).
"""

from __future__ import annotations

import asyncio
import logging

from app.config import get_settings, live_mode

logger = logging.getLogger(__name__)

__all__ = [
    "database_unreachable_reason",
    "degraded_bindings",
    "is_degraded_binding",
    "log_resolved_bindings",
    "null_in_live",
]

# A health probe that can hang is worse than one that lies: the ALB would hold the
# check open, and a burst of checks against a dead server would pin one worker each.
DATABASE_PROBE_TIMEOUT_SECONDS = 2.0


def _degraded_types() -> tuple[type, ...]:
    from app.channels.outlook_sender import NullOutlookSender
    from app.channels.twilio_sender import NullSmsSender
    from app.channels.whatsapp_adapter import NullMetaSender
    from app.connections.secrets import LocalSecretStore
    from app.notifications.email_sender import NullEmailSender
    from app.tools.calendar_backend import InMemoryCalendarApi

    return (
        NullEmailSender,
        NullSmsSender,
        NullMetaSender,
        NullOutlookSender,
        LocalSecretStore,
        InMemoryCalendarApi,
    )


def _null_sender_types() -> tuple[type, ...]:
    from app.channels.outlook_sender import NullOutlookSender
    from app.channels.twilio_sender import NullSmsSender
    from app.channels.whatsapp_adapter import NullMetaSender
    from app.notifications.email_sender import NullEmailSender

    return (NullEmailSender, NullSmsSender, NullMetaSender, NullOutlookSender)


def is_degraded_binding(obj: object) -> bool:
    """True when ``obj`` is a Null / in-process stand-in rather than a real provider."""
    return isinstance(obj, _degraded_types())


def null_in_live(sender: object) -> bool:
    """True when a message-delivery seam resolved to a Null sender in live mode.

    The Null senders return ``None`` in live mode so failure propagates on its own; this
    predicate is the second layer, letting a caller refuse before it even attempts the
    send and report a specific reason instead of a generic provider rejection.
    """
    return live_mode() and isinstance(sender, _null_sender_types())


def degraded_bindings() -> list[str]:
    """Every seam currently bound to a Null / in-process implementation."""
    from app.channels.twilio_sender import build_sms_sender
    from app.connections.secrets import get_secret_store
    from app.notifications.email_sender import build_email_sender
    from app.tools.calendar_backend import build_calendar_api

    settings = get_settings()
    # No ``meta_sender`` seam: since P1-1 the Graph token is per-tenant, so there is no
    # process-wide Meta binding to report on. Whether a given tenant can send is a
    # property of their channel_auth connection and surfaces on that connection's status,
    # not here — listing it would report every deployment as degraded forever.
    seams: tuple[tuple[str, object], ...] = (
        ("platform_email", build_email_sender()),
        ("platform_sms", build_sms_sender()),
        ("secret_store", get_secret_store()),
        ("calendar_api", build_calendar_api()),
    )
    degraded = [f"{seam}:{type(obj).__name__}" for seam, obj in seams if is_degraded_binding(obj)]
    # The gateway singleton is read from config rather than built: constructing it here
    # would bind the process-wide instance as a side effect of a health check.
    if settings.stripe_integration_posture != "test":
        degraded.append("stripe_gateway:FakeStripeGateway")
    if not live_mode():
        degraded.append("llm:EchoModel")
    # Round 5B item 17: hCaptcha in live mode. With no secret configured, captcha
    # enforcement is OFF and live signups run unprotected — a binding problem
    # this list exists to surface. (A configured-but-failing hCaptcha service
    # fails signups CLOSED and is visible in logs; a static health check cannot
    # probe it without spending a verification.)
    if live_mode() and not settings.hcaptcha_secret_key:
        degraded.append("hcaptcha:unconfigured")
    # §3.1.5 / §4.5: the abuse ceilings are only platform-wide when the Redis store is
    # bound. A live task counting in-process still enforces them, but per task and
    # reset on every deploy — a degraded binding this list exists to surface.
    if live_mode():
        from app.channels import rate_limit  # noqa: PLC0415 — avoid import cycle

        if rate_limit.store_kind() != "redis":
            degraded.append("rate_limit_store:InMemory")
    return degraded


async def database_unreachable_reason() -> str | None:
    """``None`` when a query actually round-trips to Postgres; otherwise a short reason.

    Every other seam above is answered by looking at what got BOUND, which is a purely
    process-local fact. The database is the one dependency whose binding proves nothing
    about whether it works: the URL is always well-formed, the engine constructs
    happily, and the first symptom of an unreachable server is a request that hangs
    until the caller gives up. Dev proved the gap — with the instance stopped,
    ``/healthz`` answered ``ok`` in milliseconds while every real request died on an
    asyncpg connect timeout thirty seconds later.

    Reports the exception TYPE and nothing else: a driver's message can carry the host,
    the user, and the database name, and this endpoint is unauthenticated (§3.8.3).
    """
    from sqlalchemy import text  # noqa: PLC0415 — import cost stays off module import

    from app.db.engine import get_sessionmaker  # noqa: PLC0415

    try:
        async with asyncio.timeout(DATABASE_PROBE_TIMEOUT_SECONDS):
            async with get_sessionmaker()() as session:
                await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — ANY failure to query means unreachable;
        # a probe that re-raises turns a degraded report into a 500 with a stack trace.
        reason = type(exc).__name__
        logger.warning("healthz_database_unreachable reason=%s", reason)
        return reason
    return None


def log_resolved_bindings() -> None:
    """State the resolved mode and every degraded binding at startup.

    Live mode with a degraded binding is the dangerous combination — a real deployment
    silently fabricating sends — so it logs at WARNING; anywhere else it is the expected
    posture and logs at INFO.
    """
    mode = get_settings().integration_mode
    degraded = degraded_bindings()
    if live_mode() and degraded:
        logger.warning(
            "integration_mode=%s resolved with DEGRADED bindings: %s — these seams "
            "fabricate or no-op instead of reaching a provider",
            mode,
            ", ".join(degraded),
        )
        return
    logger.info("integration_mode=%s degraded_bindings=%s", mode, ", ".join(degraded) or "none")


async def escalation_backlog() -> dict[str, int] | None:
    """Escalation notifications nobody has heard yet, platform-wide (audit F079).

    ``queued`` — retries the deferred queue still owns; ``failed24h`` — terminal misses
    in the last day. Read through the SECURITY DEFINER ``platform_escalation_backlog()``
    (migration 0057) so the app role never gains cross-tenant SELECT. ``None`` when the
    count could not be taken — an unknown backlog is reported as unknown, never as 0.
    """
    from sqlalchemy import text  # noqa: PLC0415

    from app.db.session import untenanted_session  # noqa: PLC0415

    try:
        async with untenanted_session() as s:
            row = (
                await s.execute(
                    text("SELECT queued, failed_24h FROM platform_escalation_backlog()")
                )
            ).one()
    except Exception:  # noqa: BLE001 — health must not raise
        logger.warning("escalation backlog could not be read", exc_info=True)
        return None
    return {"queued": int(row[0] or 0), "failed24h": int(row[1] or 0)}
