"""Escalation-contact confirmation, for every channel (round 7 WP-4b).

The email loop (round 5B item 13) proved an address before a hot lead depended on it;
a phone number had nothing — the "test text" persisted no state, and Twilio accepting
a request was read as the contact hearing. This module is the channel-agnostic state
over ``models.email_incident.EscalationContactConfirmation``:

* ``email`` rows MIRROR the link loop (``delivery_incidents`` writes them when a link
  is sent and when it is clicked), so one table answers "is this contact proven?" for
  both kinds;
* ``sms`` rows carry the 6-digit code loop: ``begin_sms_confirmation`` sends the code
  through the tenant's own number (the escalation SMS leg, so a tenant with no number
  connected gets the same honest ``sms_channel_not_provisioned`` refusal a page would),
  and ``confirm_sms_code`` consumes it — entered on the dashboard, or texted back to
  the number that sent it (``pipeline`` recognises the reply).

Until a contact is confirmed, ``escalation_dispatch`` copies the account owner on every
page to it. Nothing here fabricates a send: a refused code leaves the row without a
code and the refusal reason is what the dashboard shows.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.channels.base import DeliveryReceipt
from app.models.audit import ActorType, AdminAuditLog
from app.models.email_incident import EscalationContactConfirmation

KIND_EMAIL = "email"
KIND_SMS = "sms"

#: A code answers for this long; after that the owner asks for a new one.
CODE_TTL = timedelta(minutes=15)
#: Wrong entries before the outstanding code is dead (a fresh code resets the count).
MAX_CODE_ATTEMPTS = 5
#: A code (or link) is not re-sent inside this window — the resend route's cooldown.
RESEND_COOLDOWN = timedelta(minutes=5)
#: How long a page to a number makes a text FROM that number a reply rather than a
#: customer message (pipeline correlation, item 5).
PAGE_REPLY_WINDOW = timedelta(hours=24)

_CODE_RE = re.compile(r"^\D*(\d{6})\D*$")


def normalize_number(value: str) -> str:
    """+digits — the match is on digits, never on how the owner typed it."""
    return "".join(ch for ch in value if ch.isdigit() or ch == "+")


def normalize(kind: str, value: str) -> str:
    if kind == KIND_SMS:
        return normalize_number(value)
    return value.strip().lower()


def extract_code(body: str) -> str | None:
    """The 6-digit code in a texted reply, or None when the text is anything else."""
    match = _CODE_RE.match(body.strip()) if body else None
    return match.group(1) if match else None


def _hash_code(admin_id: uuid.UUID, value_normalized: str, code: str) -> str:
    return hashlib.sha256(f"{admin_id}:{value_normalized}:{code}".encode()).hexdigest()


async def states_for(
    session: AsyncSession, admin_id: uuid.UUID, contacts: Iterable[tuple[str, str]]
) -> dict[tuple[str, str], EscalationContactConfirmation]:
    """Confirmation rows for these (kind, value) contacts, keyed by (kind, normalized)."""
    wanted = {(kind, normalize(kind, value)) for kind, value in contacts if value}
    if not wanted:
        return {}
    rows = (
        (
            await session.execute(
                select(EscalationContactConfirmation).where(
                    EscalationContactConfirmation.admin_id == admin_id,
                    EscalationContactConfirmation.value_normalized.in_([v for _, v in wanted]),
                )
            )
        )
        .scalars()
        .all()
    )
    return {(r.kind, r.value_normalized): r for r in rows if (r.kind, r.value_normalized) in wanted}


async def state_for(
    session: AsyncSession, admin_id: uuid.UUID, kind: str, value: str
) -> EscalationContactConfirmation | None:
    return (await states_for(session, admin_id, [(kind, value)])).get(
        (kind, normalize(kind, value))
    )


def is_confirmed(row: EscalationContactConfirmation | None) -> bool:
    return row is not None and row.confirmed_at is not None


async def upsert(
    session: AsyncSession, admin_id: uuid.UUID, kind: str, value: str
) -> EscalationContactConfirmation:
    row = await state_for(session, admin_id, kind, value)
    if row is None:
        row = EscalationContactConfirmation(
            admin_id=admin_id, kind=kind, value_normalized=normalize(kind, value)
        )
        session.add(row)
        await session.flush()
    return row


async def mark_confirmed(
    session: AsyncSession,
    admin_id: uuid.UUID,
    kind: str,
    value: str,
    *,
    now: datetime | None = None,
) -> EscalationContactConfirmation:
    """The contact proved it can hear a page (link clicked / code accepted)."""
    row = await upsert(session, admin_id, kind, value)
    row.confirmed_at = now or datetime.now(UTC)
    row.code_hash = None
    row.code_expires_at = None
    row.attempts = 0
    await session.flush()
    return row


async def note_sent(
    session: AsyncSession,
    admin_id: uuid.UUID,
    kind: str,
    value: str,
    *,
    now: datetime | None = None,
) -> EscalationContactConfirmation:
    """A confirmation link/code went out (the email mirror; SMS records its own)."""
    row = await upsert(session, admin_id, kind, value)
    row.last_code_sent_at = now or datetime.now(UTC)
    await session.flush()
    return row


@dataclass(frozen=True)
class SmsConfirmationStart:
    """What ``begin_sms_confirmation`` did. ``sent`` is True only when the tenant's own
    number accepted the code; ``reason`` names why not (``already_confirmed``,
    ``pending`` — a live code is outstanding, ``cooldown``, or the SMS leg's refusal
    such as ``sms_channel_not_provisioned``)."""

    sent: bool
    reason: str | None = None
    receipt: DeliveryReceipt | None = None


def _code_body(code: str, business_name: str | None) -> str:
    who = f"{business_name}'s Luciel" if business_name else "a Luciel"
    return (
        f"VantageMind: {code} is your code to confirm this number as an escalation "
        f"contact for {who}. Reply with the code or enter it on the dashboard. "
        "Not you? Ignore this."
    )


async def begin_sms_confirmation(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    number: str,
    business_name: str | None = None,
    force: bool = False,
    now: datetime | None = None,
    actor_user_id: uuid.UUID | None = None,
) -> SmsConfirmationStart:
    """Send (or re-send) the 6-digit code to an SMS escalation contact through the
    tenant's own number. ``force`` is the owner's explicit resend: it re-mints even
    while a code is outstanding, but never inside the cooldown. Audited either way —
    a refusal is the record the dashboard shows, never a fabricated send."""
    from app.notifications.sms_notifier import SmsNotifier  # noqa: PLC0415

    moment = now or datetime.now(UTC)
    row = await upsert(session, admin_id, KIND_SMS, number)
    if row.confirmed_at is not None:
        return SmsConfirmationStart(sent=False, reason="already_confirmed")
    if row.last_code_sent_at is not None and moment - row.last_code_sent_at < RESEND_COOLDOWN:
        return SmsConfirmationStart(sent=False, reason="cooldown")
    if (
        not force
        and row.code_hash is not None
        and row.code_expires_at is not None
        and row.code_expires_at > moment
        and row.attempts < MAX_CODE_ATTEMPTS
    ):
        return SmsConfirmationStart(sent=False, reason="pending")

    code = f"{secrets.randbelow(10**6):06d}"
    receipt = await SmsNotifier().send_text(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        to=number,
        body=_code_body(code, business_name),
    )
    if receipt.accepted:
        row.code_hash = _hash_code(admin_id, row.value_normalized, code)
        row.code_expires_at = moment + CODE_TTL
        row.last_code_sent_at = moment
        row.attempts = 0
        await session.flush()
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        actor_type=ActorType.admin_user if actor_user_id else ActorType.platform,
        actor_user_id=actor_user_id,
        event_type="escalation_contact_verification_sent",
        payload={
            "channel": KIND_SMS,
            "number_suffix": row.value_normalized[-4:],
            "delivered": receipt.accepted,
            "detail": receipt.detail,
        },
    )
    if receipt.accepted:
        return SmsConfirmationStart(sent=True, receipt=receipt)
    return SmsConfirmationStart(sent=False, reason=receipt.detail or "refused", receipt=receipt)


#: ``confirm_sms_code`` outcomes.
CONFIRMED = "confirmed"
ALREADY_CONFIRMED = "already_confirmed"
NO_PENDING = "no_pending"
EXPIRED = "expired"
LOCKED = "locked"
WRONG_CODE = "wrong_code"


async def confirm_sms_code(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    number: str,
    code: str,
    now: datetime | None = None,
) -> str:
    """Consume the outstanding code for ``number``. A wrong entry counts; the fifth
    kills the code (``locked``) until a new one is sent. Constant-time compare."""
    moment = now or datetime.now(UTC)
    row = await state_for(session, admin_id, KIND_SMS, number)
    if row is None:
        return NO_PENDING
    if row.confirmed_at is not None:
        return ALREADY_CONFIRMED
    if row.code_hash is None or row.code_expires_at is None:
        return NO_PENDING
    if row.code_expires_at <= moment:
        return EXPIRED
    if row.attempts >= MAX_CODE_ATTEMPTS:
        return LOCKED
    presented = _hash_code(admin_id, row.value_normalized, code.strip())
    if not hmac.compare_digest(presented, row.code_hash):
        row.attempts += 1
        await session.flush()
        return LOCKED if row.attempts >= MAX_CODE_ATTEMPTS else WRONG_CODE
    await mark_confirmed(session, admin_id, KIND_SMS, number, now=moment)
    return CONFIRMED


async def code_outstanding(
    session: AsyncSession, admin_id: uuid.UUID, number: str, *, now: datetime | None = None
) -> bool:
    """Does this number still owe a live, unlocked confirmation code? What makes a
    6-digit text from it the code rather than a customer message (item 5)."""
    row = await state_for(session, admin_id, KIND_SMS, number)
    moment = now or datetime.now(UTC)
    return (
        row is not None
        and row.confirmed_at is None
        and row.code_hash is not None
        and row.code_expires_at is not None
        and row.code_expires_at > moment
        and row.attempts < MAX_CODE_ATTEMPTS
    )


async def recent_page_to_number(
    session: AsyncSession,
    admin_id: uuid.UUID,
    number: str,
    *,
    now: datetime | None = None,
    within: timedelta = PAGE_REPLY_WINDOW,
) -> bool:
    """Was this number paged by SMS inside the window? Read from the audit trail the
    dispatcher already writes (``escalation_notification_sent``, delivery_method sms),
    so a reply correlates with a real page and a stray text from a contact's phone is
    a customer message like any other (item 5)."""
    wanted = normalize_number(number)
    if not wanted:
        return False
    cutoff = (now or datetime.now(UTC)) - within
    rows = (
        (
            await session.execute(
                select(AdminAuditLog.event_payload)
                .where(
                    AdminAuditLog.admin_id == admin_id,
                    AdminAuditLog.event_type == "escalation_notification_sent",
                    AdminAuditLog.created_at >= cutoff,
                )
                .order_by(AdminAuditLog.seq.desc())
                .limit(200)
            )
        )
        .scalars()
        .all()
    )
    for payload in rows:
        if not isinstance(payload, dict) or payload.get("delivery_method") != KIND_SMS:
            continue
        recipient = payload.get("recipient")
        if isinstance(recipient, str) and normalize_number(recipient) == wanted:
            return True
    return False
