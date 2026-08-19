"""Escalation notification routing + delivery (Architecture §3.5.3 / §3.5.5).

A fired escalation is worthless if nobody hears about it: CJ Phase 6 promises the owner
learns about a hot lead **without logging in**. The escalation row is written first and is
the authoritative record (§3.5.2); this module is the best-effort delivery pass over it.

Routing (§3.5.3) is a flat per-signal map, not a chain walker — one account owner, so:

- the per-signal routing rule picks the channel, else ``preferredChannel``, else email;
- the primary contact for that channel is paged, and the secondary contact is the
  fallback when the primary is missing or its send fails;
- ``ccOwnerEmail`` on the rule fans out an extra copy to the account owner's email;
- if the configured channel delivers nothing, the platform falls back to the owner's
  email and records ``escalation_owner_fallback`` (§3.5.4).

Nothing here raises into the runtime: the customer's reply has already been sent and must
never be blocked on admin notification (§3.5.4). An undeliverable notification is loud in
the audit log (``escalation_delivery_failed`` with an actionable reason) rather than silent.

Loud, however, is not the same as delivered. A transient SES or Twilio blip used to end
the story here: one attempt per contact, an audit row nobody reads, and the CJ Phase 6
promise — the owner hears about a hot lead without logging in — quietly broken (P1-6). A
failure that could plausibly clear on its own is now handed to the rule-D deferred queue
(§3.8.7), which retries it with backoff and, at the 24h TTL, records the terminal miss as
``escalation_delivery_failed`` rather than letting it evaporate.

The distinction the enqueue turns on is whether waiting could possibly help. A provider
that refused this second may accept in ten minutes; an escalation config with no contact
in it will look exactly the same tomorrow, so that one is reported once and not queued.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.channels.base import DeliveryReceipt
from app.config import get_settings
from app.models.audit import ActorType
from app.models.conversation import EscalationEvent, Transcript
from app.models.conversation import Session as SessionModel
from app.models.instance import Account, Instance
from app.models.lead import Lead
from app.notifications.base import EscalationNotification, NotificationAdapter
from app.notifications.email_notifier import EmailNotifier
from app.notifications.sms_notifier import SmsNotifier
from app.schemas_api.luciel import EscalationContactWire

_LOG = logging.getLogger(__name__)

DEFAULT_CHANNEL = "email"

#: The deferred-queue ``action_type`` a retried escalation notification travels under.
ESCALATION_RETRY_ACTION = "escalation_notification"


@dataclass(frozen=True)
class Routing:
    """The resolved §3.5.3 routing decision for one fired signal."""

    channel: str
    primary: str | None
    secondary: str | None
    cc_owner_email: bool


def resolve_routing(escalation_config: Mapping[str, object] | None, signal: str) -> Routing:
    """Resolve (signal → contacts + channel) from the admin's escalation pillar config.

    The config is stored as the camelCase wire object the admin PUT, so it is read back
    through the same wire model rather than by hand-indexing camelCase keys.
    """
    cfg = EscalationContactWire.model_validate(dict(escalation_config or {}))
    rule = next((r for r in (cfg.routing or []) if r.signal == signal), None)
    channel = (rule.channel if rule else None) or cfg.preferred_channel or DEFAULT_CHANNEL
    if channel == "sms":
        primary, secondary = cfg.primary_sms, cfg.secondary_sms
    else:
        primary, secondary = cfg.primary_email, cfg.secondary_email
    return Routing(
        channel=channel,
        primary=primary or None,
        secondary=secondary or None,
        cc_owner_email=bool(rule.cc_owner_email) if rule else False,
    )


def has_configured_escalation_contact(escalation_config: Mapping[str, object] | None) -> bool:
    """Whether the admin has set up an actual escalation contact (§3.5.3), as opposed to
    relying on the owner-account-email last resort (§3.5.4).

    Item 4 (harmony fix 2026-08-04): the customer-facing handoff ack used to say
    "connecting you with a person now" unconditionally, even when this returns False and
    the only thing that will happen is a best-effort email to the owner's own account
    address whenever they next check it. Callers use this to choose honest ack copy and
    the dashboard uses it to decide whether to show a "configure escalation contacts"
    nudge.
    """
    cfg = EscalationContactWire.model_validate(dict(escalation_config or {}))
    return bool(cfg.primary_email or cfg.primary_sms or cfg.secondary_email or cfg.secondary_sms)


_ADAPTERS: dict[str, NotificationAdapter] = {"email": EmailNotifier(), "sms": SmsNotifier()}


async def _deliver(
    session: AsyncSession,
    notification: EscalationNotification,
    *,
    channel: str,
    to: str,
    role: str,
) -> bool:
    """One attempt to one contact, fully audited (§3.5.5). Returns whether it landed."""
    adapter = _ADAPTERS[channel]
    if not adapter.verify_config(to):
        receipt = DeliveryReceipt(accepted=False, detail="malformed_contact")
    else:
        receipt = await adapter.send(session, notification, to=to)
    await audit.emit(
        session,
        admin_id=notification.admin_id,
        instance_id=notification.instance_id,
        event_type=(
            "escalation_notification_sent" if receipt.accepted else "escalation_delivery_failed"
        ),
        actor_type=ActorType.platform,
        # Non-identifying lead reference only (§5.2 / §3.4.11) — the recipient is the
        # ADMIN's own configured contact, which is what makes the record auditable.
        payload={
            "signal": notification.signal,
            "gate": notification.gate,
            "conversation_ref": notification.conversation_ref,
            "delivery_method": channel,
            "recipient": to,
            "recipient_role": role,
            "provider_message_id": receipt.provider_message_id,
            "failure_reason": receipt.detail if not receipt.accepted else None,
        },
    )
    return receipt.accepted


async def _build_notification(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    session_id: uuid.UUID,
    resolved_lead_id: uuid.UUID | None,
    signal: str,
    gate: str,
    score: float | None,
) -> tuple[EscalationNotification, Instance | None, str | None]:
    inst = (
        await session.execute(select(Instance).where(Instance.id == instance_id))
    ).scalar_one_or_none()
    owner_email = (
        await session.execute(select(Account.email).where(Account.id == admin_id))
    ).scalar_one_or_none()
    lead = None
    if resolved_lead_id is not None:
        lead = (
            await session.execute(
                select(Lead).where(Lead.admin_id == admin_id, Lead.id == resolved_lead_id)
            )
        ).scalar_one_or_none()

    # Enrichment (2026-08-18): channel/when/who/what + a dashboard deep link, ALL
    # derived here at send time from rows already written — the deferred-queue
    # payload is unchanged, so replays render identically and no PII enters the
    # queue. The live email previously carried nothing but a bare UUID while the
    # caller's number sat in the session.
    sess_row = (
        await session.execute(
            select(SessionModel).where(
                SessionModel.admin_id == admin_id, SessionModel.id == session_id
            )
        )
    ).scalar_one_or_none()
    triggering_message = (
        await session.execute(
            select(Transcript.text)
            .where(
                Transcript.admin_id == admin_id,
                Transcript.session_id == session_id,
                Transcript.role == "lead",
            )
            .order_by(Transcript.at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    fired_at = (
        await session.execute(
            select(EscalationEvent.fired_at)
            .where(
                EscalationEvent.admin_id == admin_id,
                EscalationEvent.session_id == session_id,
                EscalationEvent.signal == signal,
            )
            .order_by(EscalationEvent.fired_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    # The customer's transport identity — only where the channel PROVIDES one
    # (voice/SMS number, email address, Meta id via the lead row). Widget visitors
    # are genuinely anonymous unless they volunteered contact info (then the lead
    # row carries it and lead_contact serves it).
    customer_identifier = lead.contact_identifier if lead else None
    base = get_settings().app_base_url.rstrip("/")
    dashboard_url = f"{base}/dashboard/conversations?open={session_id}" if base else None

    notification = EscalationNotification(
        admin_id=admin_id,
        instance_id=instance_id,
        session_id=session_id,
        signal=signal,
        gate=gate,
        business_name=(inst.name if inst and inst.name else "Your business"),
        lead_name=lead.name if lead else None,
        lead_contact=lead.contact_identifier if lead else None,
        lead_intent=lead.intent if lead else None,
        # The escalation's own score is the live one; the lead row's is capture-time.
        lead_score=score if score is not None else (lead.lead_score if lead else None),
        channel=sess_row.channel if sess_row else None,
        fired_at=fired_at,
        customer_identifier=customer_identifier,
        triggering_message=triggering_message,
        dashboard_url=dashboard_url,
    )
    return notification, inst, owner_email


async def notify_escalation(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    session_id: uuid.UUID,
    resolved_lead_id: uuid.UUID | None,
    signal: str,
    gate: str,
    score: float | None = None,
    queue_retry: bool = True,
) -> bool:
    """Notify the configured contact(s) that an escalation fired (§3.5.3).

    Returns whether anything reached anyone. ``queue_retry=False`` is what the deferred
    sweep passes when it is REPLAYING a queued notification: the queue already owns the
    backoff, and a retry that re-enqueues on failure would fork a new 24h TTL on every
    pass and never abandon.
    """
    notification, inst, owner_email = await _build_notification(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        session_id=session_id,
        resolved_lead_id=resolved_lead_id,
        signal=signal,
        gate=gate,
        score=score,
    )
    routing = resolve_routing(inst.escalation if inst else None, signal)

    delivered = False
    attempted: set[tuple[str, str]] = set()
    for role, contact in (("primary", routing.primary), ("secondary", routing.secondary)):
        if not contact:
            continue
        attempted.add((routing.channel, contact))
        delivered = await _deliver(
            session, notification, channel=routing.channel, to=contact, role=role
        )
        if delivered:
            break

    # cc-owner-email is fan-out, not fallback: it goes out even on a successful primary.
    if routing.cc_owner_email and owner_email and ("email", owner_email) not in attempted:
        attempted.add(("email", owner_email))
        await _deliver(
            session, notification, channel="email", to=owner_email, role="cc_owner_email"
        )

    if delivered:
        return True
    # Nothing reached the configured contacts — fall back to the account owner (§3.5.4).
    if owner_email and ("email", owner_email) not in attempted:
        # Recorded BEFORE the attempt, because ``attempted`` now decides more than
        # dedupe: an empty set below means "there was nowhere to send this", and a
        # fallback that was tried and refused is a refusal, not an absence.
        attempted.add(("email", owner_email))
        if await _deliver(
            session, notification, channel="email", to=owner_email, role="owner_fallback"
        ):
            await audit.emit(
                session,
                admin_id=admin_id,
                instance_id=instance_id,
                event_type="escalation_owner_fallback",
                actor_type=ActorType.platform,
                payload={
                    "signal": signal,
                    "gate": gate,
                    "conversation_ref": notification.conversation_ref,
                    "reason": (
                        "no_contact_configured"
                        if not (routing.primary or routing.secondary)
                        else "configured_contacts_undeliverable"
                    ),
                    "configured_channel": routing.channel,
                },
            )
            return True
    if not attempted:
        # No contact anywhere, not even an owner email: record it as a failure so the
        # gap is visible instead of an escalation that silently notified nobody. NOT
        # queued — an empty escalation config is not a transient condition, and retrying
        # it for 24h would bury the real failures under noise nobody can act on.
        await audit.emit(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            event_type="escalation_delivery_failed",
            actor_type=ActorType.platform,
            payload={
                "signal": signal,
                "gate": gate,
                "conversation_ref": notification.conversation_ref,
                "delivery_method": routing.channel,
                "failure_reason": "no_contact_configured",
            },
        )
        return False

    if queue_retry:
        # Contacts existed and every send was refused — the provider blip case (P1-6).
        from app.connections import deferred_queue

        await deferred_queue.enqueue(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            action_type=ESCALATION_RETRY_ACTION,
            connection_type=deferred_queue.PLATFORM_NOTIFICATION,
            resolved_lead_id=resolved_lead_id,
            payload={
                "session_id": str(session_id),
                "signal": signal,
                "gate": gate,
                "score": score,
            },
        )
    return False


async def replay_queued_notification(
    session: AsyncSession,
    action_payload: Mapping[str, object],
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    resolved_lead_id: uuid.UUID | None,
) -> bool:
    """Re-run a notification the deferred queue is holding. Never re-enqueues."""
    session_id = action_payload.get("session_id")
    signal = action_payload.get("signal")
    gate = action_payload.get("gate")
    if not isinstance(session_id, str) or not isinstance(signal, str) or not isinstance(gate, str):
        return False
    score = action_payload.get("score")
    return await notify_escalation(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        session_id=uuid.UUID(session_id),
        resolved_lead_id=resolved_lead_id,
        signal=signal,
        gate=gate,
        score=score if isinstance(score, (int, float)) else None,
        queue_retry=False,
    )


async def notify_escalation_safely(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    session_id: uuid.UUID,
    resolved_lead_id: uuid.UUID | None,
    signal: str,
    gate: str,
    score: float | None = None,
) -> None:
    """``notify_escalation`` that can never break the conversation it is reporting on.

    §3.5.4: the customer's in-conversation experience is never blocked on admin-notification
    delivery. A bug in the notification path must not fail the lead's turn, so the escalation
    row (already written) stands alone and the failure is logged for operators.
    """
    try:
        await notify_escalation(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            session_id=session_id,
            resolved_lead_id=resolved_lead_id,
            signal=signal,
            gate=gate,
            score=score,
        )
    except Exception:  # noqa: BLE001 — never propagate into the lead's turn
        _LOG.exception(
            "escalation notification failed: signal=%s gate=%s session=%s", signal, gate, session_id
        )
