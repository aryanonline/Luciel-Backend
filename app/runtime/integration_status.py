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

import logging

from app.config import get_settings, live_mode

logger = logging.getLogger(__name__)

__all__ = [
    "degraded_bindings",
    "is_degraded_binding",
    "log_resolved_bindings",
    "null_in_live",
]


def _degraded_types() -> tuple[type, ...]:
    from app.channels.twilio_sender import NullSmsSender
    from app.channels.whatsapp_adapter import NullMetaSender
    from app.connections.secrets import LocalSecretStore
    from app.notifications.email_sender import NullEmailSender
    from app.tools.calendar_backend import InMemoryCalendarApi

    return (
        NullEmailSender,
        NullSmsSender,
        NullMetaSender,
        LocalSecretStore,
        InMemoryCalendarApi,
    )


def _null_sender_types() -> tuple[type, ...]:
    from app.channels.twilio_sender import NullSmsSender
    from app.channels.whatsapp_adapter import NullMetaSender
    from app.notifications.email_sender import NullEmailSender

    return (NullEmailSender, NullSmsSender, NullMetaSender)


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
    from app.channels.meta_sender import build_meta_sender
    from app.channels.twilio_sender import build_sms_sender
    from app.connections.secrets import get_secret_store
    from app.notifications.email_sender import build_email_sender
    from app.tools.calendar_backend import build_calendar_api

    settings = get_settings()
    seams: tuple[tuple[str, object], ...] = (
        ("platform_email", build_email_sender()),
        ("platform_sms", build_sms_sender()),
        ("meta_sender", build_meta_sender()),
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
    return degraded


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
