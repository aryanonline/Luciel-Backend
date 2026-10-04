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

import asyncio
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session as SyncSession

from app.audit import log as audit
from app.channels.base import DeliveryReceipt
from app.config import get_settings
from app.models.audit import ActorType, AdminAuditLog
from app.models.conversation import EscalationEvent, Transcript
from app.models.conversation import Session as SessionModel
from app.models.instance import Account, Instance
from app.models.lead import Lead
from app.notifications.base import (
    SAFETY_CATEGORIES,
    EscalationNotification,
    NotificationAdapter,
    safety_category_from_reasons,
)
from app.notifications.email_notifier import EmailNotifier
from app.notifications.sms_notifier import SmsNotifier
from app.runtime import team_availability as team_hours
from app.schemas_api.luciel import EscalationContactWire

_LOG = logging.getLogger(__name__)

DEFAULT_CHANNEL = "email"

#: The deferred-queue ``action_type`` a retried escalation notification travels under.
ESCALATION_RETRY_ACTION = "escalation_notification"

#: In-process retry ladder (2026-09-05 audit F065): seconds after a refusal at which
#: THIS process tries again before the hourly deferred sweep takes over. A hot lead
#: cannot wait an hour on a provider blip. Tests set it to () or (0,).
ESCALATION_RETRY_DELAYS: tuple[float, ...] = (30.0, 120.0)
_BACKGROUND_RETRIES: set[asyncio.Task[None]] = set()

#: ``escalation_events.delivery_status`` vocabulary (migration 0057).
DELIVERY_PENDING = "pending"
DELIVERY_DELIVERED = "delivered"  # a CONFIGURED contact heard
DELIVERY_OWNER_FALLBACK = "owner_fallback"  # only the account owner heard so far
DELIVERY_QUEUED = "queued"  # nobody yet; the deferred queue / ladder own it
DELIVERY_FAILED = "failed"  # terminal
#: Round 7 WP-4b: the provider accepted the page and the carrier has yet to report —
#: an SMS with a delivery receipt outstanding. Not a retry candidate; ``delivered`` is
#: stamped by the receipt (``record_sms_delivery_receipt``), never by the API accept.
DELIVERY_SENT = "sent"
#: Statuses under which nothing may page the configured contact again.
_SETTLED = frozenset({DELIVERY_DELIVERED, DELIVERY_SENT})

#: Refusals that look exactly the same tomorrow (round 7 WP-4b, item 6): a contact
#: that could never be an address or a number, and an SMS contact with no number
#: connected to send from. Reported once and stamped terminal — never queued for 24h.
TERMINAL_REFUSALS = frozenset({"malformed_contact", "sms_channel_not_provisioned"})


@dataclass(frozen=True)
class Routing:
    """The resolved §3.5.3 routing decision for one fired signal."""

    channel: str
    primary: str | None
    secondary: str | None
    cc_owner_email: bool
    # Round 6 WP-E: the team was unreachable and the owner's after-hours contact took
    # the page; ``next_reachable`` is when a regular person is back (owner's zone).
    after_hours: bool = False
    next_reachable: datetime | None = None


def resolve_routing(
    escalation_config: Mapping[str, object] | None,
    signal: str,
    *,
    team_availability: Mapping[str, object] | None = None,
    timezone: str | None = None,
    now: datetime | None = None,
) -> Routing:
    """Resolve (signal → contacts + channel) from the admin's escalation pillar config.

    The config is stored as the camelCase wire object the admin PUT, so it is read back
    through the same wire model rather than by hand-indexing camelCase keys.

    Round 6 WP-E (F054): when the owner described their team's hours, nobody is
    reachable at ``now`` and an after-hours contact is named, that contact takes the
    page on its channel; the regular contact for that channel stays as the backup so
    a failing after-hours address still tries a person before the owner fallback.
    With the setting off (or the team reachable) the resolution is unchanged.
    """
    cfg = EscalationContactWire.model_validate(dict(escalation_config or {}))
    rule = next((r for r in (cfg.routing or []) if r.signal == signal), None)
    channel = (rule.channel if rule else None) or cfg.preferred_channel or DEFAULT_CHANNEL
    if channel == "sms":
        primary, secondary = cfg.primary_sms, cfg.secondary_sms
    else:
        primary, secondary = cfg.primary_email, cfg.secondary_email
    routing = Routing(
        channel=channel,
        primary=primary or None,
        secondary=secondary or None,
        cc_owner_email=bool(rule.cc_owner_email) if rule else False,
    )
    hours = team_hours.parse(team_availability, timezone)
    if hours is None or hours.after_hours is None or team_hours.team_reachable_now(hours, now):
        return routing
    ah = hours.after_hours
    regular = (cfg.primary_sms if ah.channel == "sms" else cfg.primary_email) or None
    return Routing(
        channel=ah.channel,
        primary=ah.contact,
        secondary=regular if regular != ah.contact else None,
        cc_owner_email=(
            ah.cc_owner_email if ah.cc_owner_email is not None else routing.cc_owner_email
        ),
        after_hours=True,
        next_reachable=team_hours.next_reachable(hours, now),
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
) -> DeliveryReceipt:
    """One attempt to one contact, fully audited (§3.5.5). Returns the receipt: whether
    it landed and, when it did not, the reason (the retry decision hangs on it)."""
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
    return receipt


async def _stamp_delivery(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    session_id: uuid.UUID,
    signal: str,
    gate: str,
    status: str,
    detail: str | None = None,
    provider_message_id: str | None = None,
) -> None:
    """Record what happened to the notification on its escalation row (F079/F083).
    ``provider_message_id`` is the send the status rests on — what a delivery receipt
    later finds the row by (round 7 WP-4b)."""
    values: dict[str, object] = {
        "delivery_status": status,
        "delivery_detail": (detail or None) and str(detail)[:64],
    }
    if provider_message_id:
        values["delivery_provider_message_id"] = str(provider_message_id)[:64]
    if status == DELIVERY_DELIVERED:
        values["delivered_at"] = datetime.now(UTC)
    await session.execute(
        update(EscalationEvent)
        .where(
            EscalationEvent.admin_id == admin_id,
            EscalationEvent.session_id == session_id,
            EscalationEvent.signal == signal,
            EscalationEvent.gate == gate,
        )
        .values(**values)
    )


async def delivery_status_of(
    session: AsyncSession, *, admin_id: uuid.UUID, session_id: uuid.UUID, signal: str, gate: str
) -> str | None:
    return (
        await session.execute(
            select(EscalationEvent.delivery_status).where(
                EscalationEvent.admin_id == admin_id,
                EscalationEvent.session_id == session_id,
                EscalationEvent.signal == signal,
                EscalationEvent.gate == gate,
            )
        )
    ).scalar_one_or_none()


async def _enqueue_retry(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    session_id: uuid.UUID,
    resolved_lead_id: uuid.UUID | None,
    signal: str,
    gate: str,
    score: float | None,
    contacts_only: bool,
) -> None:
    from app.connections import deferred_queue  # noqa: PLC0415

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
            "contacts_only": contacts_only,
        },
    )


def _schedule_background_retries(
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    session_id: uuid.UUID,
    resolved_lead_id: uuid.UUID | None,
    signal: str,
    gate: str,
    score: float | None,
    contacts_only: bool,
) -> None:
    """Start the in-process ladder (F065) — never awaited by the lead's turn."""
    if not ESCALATION_RETRY_DELAYS:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(
        _background_retry(
            admin_id=admin_id,
            instance_id=instance_id,
            session_id=session_id,
            resolved_lead_id=resolved_lead_id,
            signal=signal,
            gate=gate,
            score=score,
            contacts_only=contacts_only,
        )
    )
    _BACKGROUND_RETRIES.add(task)
    task.add_done_callback(_BACKGROUND_RETRIES.discard)


async def _background_retry(
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    session_id: uuid.UUID,
    resolved_lead_id: uuid.UUID | None,
    signal: str,
    gate: str,
    score: float | None,
    contacts_only: bool,
) -> None:
    from app.db.session import tenant_session  # noqa: PLC0415

    for delay in ESCALATION_RETRY_DELAYS:
        await asyncio.sleep(delay)
        try:
            async with tenant_session(admin_id) as s:
                status = await delivery_status_of(
                    s, admin_id=admin_id, session_id=session_id, signal=signal, gate=gate
                )
                if status in _SETTLED:
                    return
                if status == DELIVERY_OWNER_FALLBACK:
                    # The owner heard through another pass (the sweep, an earlier rung):
                    # this rung is for the configured contact alone (item 7).
                    contacts_only = True
                if await notify_escalation(
                    s,
                    admin_id=admin_id,
                    instance_id=instance_id,
                    session_id=session_id,
                    resolved_lead_id=resolved_lead_id,
                    signal=signal,
                    gate=gate,
                    score=score,
                    queue_retry=False,
                    contacts_only=contacts_only,
                ):
                    return
        except Exception:  # noqa: BLE001 — the ladder is best-effort; the queue remains
            _LOG.exception(
                "escalation retry ladder attempt failed: signal=%s session=%s", signal, session_id
            )


def _awaitable_here(tasks: set[asyncio.Task[None]]) -> list[asyncio.Task[None]]:
    """The tasks in ``tasks`` that the RUNNING loop can await, dropping the rest.

    A task belongs to the loop that created it. One left behind by a loop that has
    since stopped (a test client's own loop, a worker being torn down) can never
    finish, and ``asyncio.gather`` on it from another loop waits forever — which is
    how a full-suite CI run sat for 34 minutes in a drain (round 7, 2026-10-04). Such
    a task is abandoned work, not pending work: it is dropped from the registry and
    logged, so a drain is always bounded by tasks that can actually complete.
    """
    loop = asyncio.get_running_loop()
    here: list[asyncio.Task[None]] = []
    for task in list(tasks):
        if task.get_loop() is loop:
            here.append(task)
        else:
            tasks.discard(task)
            _LOG.error("abandoned background escalation task from another event loop: %r", task)
    return here


async def drain_background_retries() -> None:
    """Await every in-process retry ladder currently running (tests / shutdown)."""
    while pending := _awaitable_here(_BACKGROUND_RETRIES):
        await asyncio.gather(*pending, return_exceptions=True)


# --- delivery OFF the customer's turn (round 7 WP-2, item 5; §3.5.4) ---------------- #
#
# The escalation row is written inside the customer's request; the owner notification
# used to be awaited right there, BEFORE the reply was even composed — with the SES and
# Twilio SDKs making blocking calls on the event loop. Architecture §3.5.4 says the
# reply is sent before the notification pipeline starts. So the notification is now
# scheduled to start AFTER the request's transaction commits (the row must be durable
# and visible from another connection before anything reads it back), runs as a
# background task in its own tenant session, and is tracked so tests and the process
# shutdown can drain it. A rolled-back request never notifies about a row that does
# not exist.
_BACKGROUND_NOTIFICATIONS: set[asyncio.Task[None]] = set()
_AFTER_COMMIT_QUEUE = "escalation_notifications_after_commit"
_AFTER_COMMIT_LISTENER = "escalation_notifications_listener"


def schedule_notification_after_commit(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    session_id: uuid.UUID,
    resolved_lead_id: uuid.UUID | None,
    signal: str,
    gate: str,
    score: float | None,
) -> None:
    """Queue the owner notification for a row this session just wrote; it starts as a
    background task once the session COMMITS (never on rollback), in its own tenant
    session, after the customer's turn has been answered (§3.5.4).

    Must be called from inside the running event loop (it is: the loop's own turn)."""
    loop = asyncio.get_running_loop()
    sync = session.sync_session
    queue: list[tuple[asyncio.AbstractEventLoop, dict[str, object]]] = sync.info.setdefault(
        _AFTER_COMMIT_QUEUE, []
    )
    queue.append(
        (
            loop,
            {
                "admin_id": admin_id,
                "instance_id": instance_id,
                "session_id": session_id,
                "resolved_lead_id": resolved_lead_id,
                "signal": signal,
                "gate": gate,
                "score": score,
            },
        )
    )
    if not sync.info.get(_AFTER_COMMIT_LISTENER):
        sync.info[_AFTER_COMMIT_LISTENER] = True
        event.listen(sync, "after_commit", _start_notifications_after_commit)


def _start_notifications_after_commit(sync_session: SyncSession) -> None:
    """SQLAlchemy ``after_commit`` hook: the row is durable, start the delivery tasks."""
    queued = sync_session.info.pop(_AFTER_COMMIT_QUEUE, [])
    for loop, kwargs in queued:
        task = loop.create_task(_notify_in_own_session(**kwargs))
        _BACKGROUND_NOTIFICATIONS.add(task)
        task.add_done_callback(_BACKGROUND_NOTIFICATIONS.discard)


async def _notify_in_own_session(
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    session_id: uuid.UUID,
    resolved_lead_id: uuid.UUID | None,
    signal: str,
    gate: str,
    score: float | None,
) -> None:
    from app.db.session import tenant_session  # noqa: PLC0415

    try:
        async with tenant_session(admin_id) as s:
            await notify_escalation_safely(
                s,
                admin_id=admin_id,
                instance_id=instance_id,
                session_id=session_id,
                resolved_lead_id=resolved_lead_id,
                signal=signal,
                gate=gate,
                score=score,
            )
    except Exception:  # noqa: BLE001 — the row stands; the sweep still sees pending
        _LOG.exception(
            "background escalation notification failed: signal=%s session=%s", signal, session_id
        )


async def drain_background_notifications() -> None:
    """Await every off-turn notification currently running (tests / shutdown)."""
    while pending := _awaitable_here(_BACKGROUND_NOTIFICATIONS):
        await asyncio.gather(*pending, return_exceptions=True)


async def drain_background_work() -> None:
    """Everything this module runs off the customer's turn: the notifications and
    the retry ladders they may start. Wired into the process shutdown."""
    while _BACKGROUND_NOTIFICATIONS or _BACKGROUND_RETRIES:
        await drain_background_notifications()
        await drain_background_retries()


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
    fired_row = (
        await session.execute(
            select(EscalationEvent.fired_at, EscalationEvent.reasons)
            .where(
                EscalationEvent.admin_id == admin_id,
                EscalationEvent.session_id == session_id,
                EscalationEvent.signal == signal,
            )
            .order_by(EscalationEvent.fired_at.desc())
            .limit(1)
        )
    ).one_or_none()
    fired_at = fired_row[0] if fired_row else None
    # The row's own reasons (F124), read here at send time like everything else — a
    # hand_to_team page (round 7 WP-2, item 2) is worthless without Luciel's reason.
    reasons = tuple(str(r) for r in (fired_row[1] or [])) if fired_row else ()
    # The customer's transport identity — only where the channel PROVIDES one
    # (voice/SMS number, email address, Meta id via the lead row). Widget visitors
    # are genuinely anonymous unless they volunteered contact info (then the lead
    # row carries it and lead_contact serves it).
    customer_identifier = lead.contact_identifier if lead else None
    # Item 3: the address / number the lead VOLUNTEERED on top of the transport key
    # (lead.email / lead.phone are NULL when contact_identifier already is it).
    lead_email = (lead.email or None) if lead else None
    lead_phone = (lead.phone or None) if lead else None
    # Item 4: a moderation take-over (self_harm / threats_violence) is a SAFETY page.
    # The row's first reason names it; a row written without the token (the runtime
    # audits ``content_moderation_event`` right before it takes over) is read back
    # from that audit trail — never rendered as "the customer asked for a person".
    safety_category = safety_category_from_reasons(reasons)
    if safety_category is None and signal == "explicit_human_request":
        safety_category = await _moderation_takeover_category(
            session, admin_id=admin_id, session_id=session_id
        )
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
        reasons=reasons,
        lead_email=lead_email,
        lead_phone=lead_phone,
        safety_category=safety_category,
    )
    return notification, inst, owner_email


async def _moderation_takeover_category(
    session: AsyncSession, *, admin_id: uuid.UUID, session_id: uuid.UUID
) -> str | None:
    """The safety category behind an ``explicit_human_request`` row written by the
    moderation take-over (item 4): the runtime audits ``content_moderation_event`` for
    the session right before it hands the conversation to a person. None for a real
    request for a person."""
    rows = (
        (
            await session.execute(
                select(AdminAuditLog.event_payload)
                .where(
                    AdminAuditLog.admin_id == admin_id,
                    AdminAuditLog.event_type == "content_moderation_event",
                    AdminAuditLog.event_payload["session_id"].astext == str(session_id),
                )
                .order_by(AdminAuditLog.seq.desc())
                .limit(5)
            )
        )
        .scalars()
        .all()
    )
    for payload in rows:
        category = payload.get("category") if isinstance(payload, dict) else None
        if isinstance(category, str) and category in SAFETY_CATEGORIES:
            return category
    return None


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
    contacts_only: bool = False,
) -> bool:
    """Notify the configured contact(s) that an escalation fired (§3.5.3).

    Returns whether anything reached anyone. ``queue_retry=False`` is what the deferred
    sweep and the in-process ladder pass when they are REPLAYING a notification: the
    queue already owns the backoff, and a retry that re-enqueues on failure would fork a
    new 24h TTL on every pass and never abandon. ``contacts_only`` is the replay of a
    notification the OWNER already received as a fallback (F083): the configured
    contact is tried again, the owner is never paged a second time for it.
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
    routing = resolve_routing(
        inst.escalation if inst else None,
        signal,
        team_availability=inst.team_availability if inst else None,
        timezone=inst.timezone if inst else None,
    )
    if routing.after_hours:
        # WP-E: say so in the audit trail — the dashboard's "who was paged" must not
        # look like the regular contact was skipped for no reason.
        await audit.emit(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            event_type="escalation_after_hours_routing",
            actor_type=ActorType.platform,
            payload={
                "signal": signal,
                "gate": gate,
                "conversation_ref": notification.conversation_ref,
                "delivery_method": routing.channel,
                "next_reachable": (
                    routing.next_reachable.isoformat() if routing.next_reachable else None
                ),
            },
        )

    # Round 5B item 13: suppression + verification state for the email contacts.
    # A hard-bounced/complained address is never attempted (SES "accepting" it
    # proved nothing — the delivery still died, silently); an address never
    # confirmed gets the owner copied so a typo can't black-hole a hot lead.
    from app.models.email_incident import (  # noqa: PLC0415
        EmailDeliveryIncident,
        is_suppressed,
        is_verified,
    )
    from app.notifications import contact_confirmation  # noqa: PLC0415
    from app.notifications.delivery_incidents import states_for  # noqa: PLC0415

    email_states = await states_for(
        session,
        admin_id,
        [c for c in (routing.primary, routing.secondary, owner_email) if c],
    )
    # Round 7 WP-4b: the same rule for a number — until the contact answered its code,
    # the owner is copied on every page to it (SMS contacts were never proven before).
    sms_states = (
        await contact_confirmation.states_for(
            session,
            admin_id,
            [(contact_confirmation.KIND_SMS, c) for c in (routing.primary, routing.secondary) if c],
        )
        if routing.channel == "sms"
        else {}
    )

    def _state_of(address: str) -> EmailDeliveryIncident | None:
        return email_states.get(address.strip().lower())

    def _proven(contact: str) -> bool:
        if routing.channel == "email":
            return is_verified(_state_of(contact))
        return contact_confirmation.is_confirmed(
            sms_states.get(
                (contact_confirmation.KIND_SMS, contact_confirmation.normalize_number(contact))
            )
        )

    delivered = False
    delivered_unverified = False
    delivered_role: str | None = None
    attempted: set[tuple[str, str]] = set()
    last_refusal: str | None = None  # a provider refusal that could clear on its own
    terminal_refusal: str | None = None  # a refusal that waiting cannot change (item 6)
    owner_fallback: DeliveryReceipt | None = None  # the owner-email last resort, if tried
    for role, contact in (("primary", routing.primary), ("secondary", routing.secondary)):
        if not contact:
            continue
        if routing.channel == "email" and is_suppressed(_state_of(contact)):
            # Refusal, not absence: counted as attempted (the owner fallback
            # below is then a real fallback), audited with the reason.
            attempted.add((routing.channel, contact))
            state = _state_of(contact)
            reason = (
                "contact_suppressed_complaint"
                if state is not None and state.complained_at is not None
                else "contact_suppressed_hard_bounce"
            )
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
                    "recipient": contact,
                    "recipient_role": role,
                    "failure_reason": reason,
                },
            )
            continue
        attempted.add((routing.channel, contact))
        receipt = await _deliver(
            session, notification, channel=routing.channel, to=contact, role=role
        )
        delivered = receipt.accepted
        if delivered:
            delivered_unverified = not _proven(contact)
            delivered_role = role
            await _stamp_delivery(
                session,
                admin_id=admin_id,
                session_id=session_id,
                signal=signal,
                gate=gate,
                # A live SMS with a delivery receipt outstanding is ``sent``; the
                # carrier's receipt makes it ``delivered`` (round 7 WP-4b).
                status=(
                    DELIVERY_SENT if receipt.detail == _sms_dlr_pending() else DELIVERY_DELIVERED
                ),
                detail=f"{routing.channel}:{role}",
                provider_message_id=receipt.provider_message_id,
            )
            break
        if receipt.detail in TERMINAL_REFUSALS:
            terminal_refusal = receipt.detail
            await _record_terminal_refusal(
                session,
                admin_id=admin_id,
                channel=routing.channel,
                contact=contact,
                reason=receipt.detail,
            )
            continue
        last_refusal = receipt.detail or "refused"

    if contacts_only:
        # The owner already heard (fallback); this replay is for the configured
        # contact alone. Nothing below may page the owner again.
        return delivered

    # cc-owner-email is fan-out, not fallback: it goes out even on a successful primary.
    if routing.cc_owner_email and owner_email and ("email", owner_email) not in attempted:
        attempted.add(("email", owner_email))
        await _deliver(
            session, notification, channel="email", to=owner_email, role="cc_owner_email"
        )

    if delivered:
        # Round 5B item 13: an UNCONFIRMED contact accepted the send — copy the
        # account owner too. SES acceptance is not delivery, and until the
        # contact proves out (or for legacy contacts saved before verification
        # existed), silence toward a typo'd address must not be trusted. Round 7
        # WP-4b: the same for a number that never answered its confirmation code.
        if delivered_unverified and owner_email and ("email", owner_email) not in attempted:
            attempted.add(("email", owner_email))
            copy = await _deliver(
                session, notification, channel="email", to=owner_email, role="owner_unverified_copy"
            )
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
                    "reason": "contact_unverified",
                    "configured_channel": routing.channel,
                },
            )
            if copy.accepted:
                # Remembered on the row: a failed delivery receipt for the contact
                # must not page the owner a second time for this escalation.
                await session.execute(
                    update(EscalationEvent)
                    .where(
                        EscalationEvent.admin_id == admin_id,
                        EscalationEvent.session_id == session_id,
                        EscalationEvent.signal == signal,
                        EscalationEvent.gate == gate,
                    )
                    .values(delivery_detail=f"{routing.channel}:{delivered_role}+owner"[:64])
                )
        return True
    # Nothing reached the configured contacts — fall back to the account owner (§3.5.4).
    if owner_email and ("email", owner_email) not in attempted:
        # Recorded BEFORE the attempt, because ``attempted`` now decides more than
        # dedupe: an empty set below means "there was nowhere to send this", and a
        # fallback that was tried and refused is a refusal, not an absence.
        attempted.add(("email", owner_email))
        if is_suppressed(_state_of(owner_email)):
            # The owner's own address is bouncing / complained (audit F089): the
            # fallback would die exactly like the contact did. Say so, and let the
            # queue keep trying the contact rather than pretending the owner heard.
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
                    "delivery_method": "email",
                    "recipient": owner_email,
                    "recipient_role": "owner_fallback",
                    "failure_reason": "owner_email_suppressed",
                },
            )
        elif (
            owner_fallback := await _deliver(
                session, notification, channel="email", to=owner_email, role="owner_fallback"
            )
        ).accepted:
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
            await _stamp_delivery(
                session,
                admin_id=admin_id,
                session_id=session_id,
                signal=signal,
                gate=gate,
                status=DELIVERY_OWNER_FALLBACK,
                detail=last_refusal or terminal_refusal or "no_contact_configured",
            )
            if not queue_retry and last_refusal is not None:
                # A REPLAY (sweep or ladder) reached the owner: the queued item for this
                # row must not page the owner again on its next pass — re-point it to
                # the configured contact alone (round 7 WP-4b, item 7).
                await _repoint_queued_to_contacts_only(
                    session, admin_id=admin_id, session_id=session_id, signal=signal, gate=gate
                )
            if queue_retry and last_refusal is not None:
                # The owner heard, but the person the owner CONFIGURED did not (audit
                # F083): keep trying that contact — contacts only, the owner is never
                # paged twice for one escalation.
                await _enqueue_retry(
                    session,
                    admin_id=admin_id,
                    instance_id=instance_id,
                    session_id=session_id,
                    resolved_lead_id=resolved_lead_id,
                    signal=signal,
                    gate=gate,
                    score=score,
                    contacts_only=True,
                )
                _schedule_background_retries(
                    admin_id=admin_id,
                    instance_id=instance_id,
                    session_id=session_id,
                    resolved_lead_id=resolved_lead_id,
                    signal=signal,
                    gate=gate,
                    score=score,
                    contacts_only=True,
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
        await _stamp_delivery(
            session,
            admin_id=admin_id,
            session_id=session_id,
            signal=signal,
            gate=gate,
            status=DELIVERY_FAILED,
            detail="no_contact_configured",
        )
        return False

    # The owner-email last resort refused by the provider is a blip like any other —
    # the tenant with no contacts configured (P1-6) is retried through it.
    owner_refusal = (
        (owner_fallback.detail or "refused")
        if owner_fallback is not None and not owner_fallback.accepted
        else None
    )
    if last_refusal is None and owner_refusal is None:
        # Every refusal was terminal (item 6): a malformed contact or an SMS contact
        # with no number to send from looks exactly the same in an hour. The owner
        # fallback above was the last resort; nothing is left to retry. Stamped
        # ``failed`` now — never a 24h queue of noise ending in the same answer.
        await _stamp_delivery(
            session,
            admin_id=admin_id,
            session_id=session_id,
            signal=signal,
            gate=gate,
            status=DELIVERY_FAILED,
            detail=terminal_refusal or "refused",
        )
        return False

    if queue_retry:
        # Contacts existed and every send was refused — the provider blip case (P1-6).
        # The deferred queue owns the long tail; the in-process ladder (F065) owns the
        # next few minutes.
        await _enqueue_retry(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            session_id=session_id,
            resolved_lead_id=resolved_lead_id,
            signal=signal,
            gate=gate,
            score=score,
            contacts_only=False,
        )
        await _stamp_delivery(
            session,
            admin_id=admin_id,
            session_id=session_id,
            signal=signal,
            gate=gate,
            status=DELIVERY_QUEUED,
            detail=last_refusal or owner_refusal or "refused",
        )
        _schedule_background_retries(
            admin_id=admin_id,
            instance_id=instance_id,
            session_id=session_id,
            resolved_lead_id=resolved_lead_id,
            signal=signal,
            gate=gate,
            score=score,
            contacts_only=False,
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
    sid = uuid.UUID(session_id)
    # The in-process ladder (F065) may already have reached the contact while this item
    # waited its backoff: a second page for the same escalation is noise, not delivery.
    status = await delivery_status_of(
        session, admin_id=admin_id, session_id=sid, signal=signal, gate=gate
    )
    if status in _SETTLED:
        return True
    # The owner already heard (the ladder's fallback landed while this item waited):
    # this pass is for the configured contact alone, whatever the item says (item 7).
    contacts_only = bool(action_payload.get("contacts_only")) or status == DELIVERY_OWNER_FALLBACK
    return await notify_escalation(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        session_id=sid,
        resolved_lead_id=resolved_lead_id,
        signal=signal,
        gate=gate,
        score=score if isinstance(score, (int, float)) else None,
        queue_retry=False,
        contacts_only=contacts_only,
    )


async def mark_abandoned(session: AsyncSession, action_payload: Mapping[str, object]) -> None:
    """The deferred queue gave up on this notification (24h TTL): the row's delivery
    truth becomes terminal — unless the owner already heard through the fallback, in
    which case ``owner_fallback`` stays the honest answer."""
    session_id = action_payload.get("session_id")
    signal = action_payload.get("signal")
    gate = action_payload.get("gate")
    if not isinstance(session_id, str) or not isinstance(signal, str) or not isinstance(gate, str):
        return
    admin_id = action_payload.get("admin_id")
    row = (
        await session.execute(
            select(EscalationEvent).where(
                EscalationEvent.session_id == uuid.UUID(session_id),
                EscalationEvent.signal == signal,
                EscalationEvent.gate == gate,
                *(
                    [EscalationEvent.admin_id == uuid.UUID(admin_id)]
                    if isinstance(admin_id, str)
                    else []
                ),
            )
        )
    ).scalar_one_or_none()
    if row is None or row.delivery_status not in (DELIVERY_PENDING, DELIVERY_QUEUED, "unknown"):
        return
    row.delivery_status = DELIVERY_FAILED
    row.delivery_detail = "abandoned_after_24h"


def _sms_dlr_pending() -> str:
    from app.notifications.sms_notifier import DLR_PENDING  # noqa: PLC0415

    return DLR_PENDING


async def _record_terminal_refusal(
    session: AsyncSession, *, admin_id: uuid.UUID, channel: str, contact: str, reason: str
) -> None:
    """A malformed EMAIL contact is recorded as a delivery incident (item 6): mail to it
    can never land, which is exactly what the dashboard's ``escalation_bouncing`` need
    and the contact-health surface already say about a hard-bounced address — the
    existing seam, so the owner is told to fix the address without a new rule. A
    number with no SMS channel behind it has no email row to speak through; the
    contact-health surface names ``sms_channel_not_provisioned`` for it instead."""
    if channel != "email" or reason != "malformed_contact":
        return
    from app.notifications.delivery_incidents import _upsert_row  # noqa: PLC0415

    row = await _upsert_row(session, admin_id, contact)
    now = datetime.now(UTC)
    if row.hard_bounced_at is None or row.last_event_detail != "malformed_contact":
        row.hard_bounced_at = now
        row.last_event_at = now
        row.last_event_detail = "malformed_contact"
        await session.flush()


async def _repoint_queued_to_contacts_only(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    session_id: uuid.UUID,
    signal: str,
    gate: str,
) -> int:
    """Mark this row's queued retry ``contacts_only`` (item 7). Returns rows changed."""
    from app.models.deferred import DeferredAction  # noqa: PLC0415

    rows = (
        (
            await session.execute(
                select(DeferredAction).where(
                    DeferredAction.admin_id == admin_id,
                    DeferredAction.action_type == ESCALATION_RETRY_ACTION,
                    DeferredAction.status == "queued",
                )
            )
        )
        .scalars()
        .all()
    )
    changed = 0
    for action in rows:
        payload = dict(action.payload or {})
        if (
            payload.get("session_id") != str(session_id)
            or payload.get("signal") != signal
            or payload.get("gate") != gate
            or payload.get("contacts_only")
        ):
            continue
        payload["contacts_only"] = True
        action.payload = payload  # a NEW dict so the JSONB change is flushed
        changed += 1
    if changed:
        await session.flush()
    return changed


#: Twilio ``MessageStatus`` values a delivery receipt settles a row with.
_DLR_DELIVERED = frozenset({"delivered", "read"})
_DLR_FAILED = frozenset({"failed", "undelivered"})


async def record_sms_delivery_receipt(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    provider_message_id: str,
    message_status: str,
    error_code: str | None = None,
) -> str:
    """Stamp a page's row from the carrier's word (round 7 WP-4b): ``delivered`` when
    Twilio says delivered; on ``failed`` / ``undelivered`` the contact did not hear,
    so the failure is audited and the owner is paged by email now — unless the owner
    was already copied when the page went out (an unproven contact), in which case the
    row simply reads ``owner_fallback``. Interim statuses are ignored. Returns what
    was done: delivered | owner_fallback | failed | ignored | unknown_message."""
    status = (message_status or "").strip().lower()
    if status not in _DLR_DELIVERED and status not in _DLR_FAILED:
        return "ignored"
    row = (
        await session.execute(
            select(EscalationEvent).where(
                EscalationEvent.admin_id == admin_id,
                EscalationEvent.delivery_provider_message_id == provider_message_id,
            )
        )
    ).scalar_one_or_none()
    if row is None or row.session_id is None:
        return "unknown_message"
    if status in _DLR_DELIVERED:
        if row.delivery_status != DELIVERY_DELIVERED:
            row.delivery_status = DELIVERY_DELIVERED
            row.delivered_at = datetime.now(UTC)
            await session.flush()
        return "delivered"
    if row.delivery_status in (DELIVERY_FAILED, DELIVERY_OWNER_FALLBACK):
        return "ignored"  # a retried receipt for a row already settled by one
    reason = f"sms_dlr:{status}" + (f":{error_code}" if error_code else "")
    notification, _inst, owner_email = await _build_notification(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        session_id=row.session_id,
        resolved_lead_id=row.resolved_lead_id,
        signal=row.signal,
        gate=row.gate,
        score=row.score_or_confidence,
    )
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        event_type="escalation_delivery_failed",
        actor_type=ActorType.platform,
        payload={
            "signal": row.signal,
            "gate": row.gate,
            "conversation_ref": notification.conversation_ref,
            "delivery_method": "sms",
            "provider_message_id": provider_message_id,
            "failure_reason": reason,
        },
    )
    if "+owner" in (row.delivery_detail or ""):
        row.delivery_status = DELIVERY_OWNER_FALLBACK
        row.delivery_detail = reason[:64]
        await session.flush()
        return "owner_fallback"
    from app.models.email_incident import is_suppressed  # noqa: PLC0415
    from app.notifications.delivery_incidents import states_for  # noqa: PLC0415

    if owner_email and not is_suppressed(
        (await states_for(session, admin_id, [owner_email])).get(owner_email.strip().lower())
    ):
        fallback = await _deliver(
            session, notification, channel="email", to=owner_email, role="owner_fallback"
        )
        if fallback.accepted:
            await audit.emit(
                session,
                admin_id=admin_id,
                instance_id=instance_id,
                event_type="escalation_owner_fallback",
                actor_type=ActorType.platform,
                payload={
                    "signal": row.signal,
                    "gate": row.gate,
                    "conversation_ref": notification.conversation_ref,
                    "reason": "sms_delivery_failed",
                    "configured_channel": "sms",
                },
            )
            row.delivery_status = DELIVERY_OWNER_FALLBACK
            row.delivery_detail = reason[:64]
            await session.flush()
            return "owner_fallback"
    row.delivery_status = DELIVERY_FAILED
    row.delivery_detail = reason[:64]
    await session.flush()
    return "failed"


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
    except Exception as exc:  # noqa: BLE001 — never propagate into the lead's turn
        _LOG.exception(
            "escalation notification failed: signal=%s gate=%s session=%s", signal, gate, session_id
        )
        # A crash in the notifier is still a hot lead nobody was told about (audit F088):
        # leave the same trail a refused send leaves, and hand it to the queue.
        try:
            await audit.emit(
                session,
                admin_id=admin_id,
                instance_id=instance_id,
                event_type="escalation_delivery_failed",
                actor_type=ActorType.platform,
                payload={
                    "signal": signal,
                    "gate": gate,
                    "session_id": str(session_id),
                    "failure_reason": f"notifier_exception:{type(exc).__name__}",
                },
            )
            await _enqueue_retry(
                session,
                admin_id=admin_id,
                instance_id=instance_id,
                session_id=session_id,
                resolved_lead_id=resolved_lead_id,
                signal=signal,
                gate=gate,
                score=score,
                contacts_only=False,
            )
            await _stamp_delivery(
                session,
                admin_id=admin_id,
                session_id=session_id,
                signal=signal,
                gate=gate,
                status=DELIVERY_QUEUED,
                detail=f"notifier_exception:{type(exc).__name__}"[:64],
            )
        except Exception:  # noqa: BLE001
            _LOG.exception(
                "escalation notification failure could not be recorded: session=%s", session_id
            )
