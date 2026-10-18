"""Channel switching with contact confirmation (round 6 WP-H, audit F060).

A customer on one channel may ask for the answer on another ("email me the
details", "text it to me"). Nothing is ever sent to an address the customer has not
confirmed: an identifier that arrived with the transport (their own phone number on
SMS, their own address on email) is confirmed at capture; one typed into the same
message as the request is theirs by construction; anything else is ASKED for, the
answer parked on the session, and sent on the next turn once they confirm or supply
it. The reply on the channel they wrote on always keeps the full answer.

A confirmed identifier is a delivery address for the conversation it came from, not
for every conversation on the lead (phase-2 security review): a number that joined a
lead through a typed claim (WP-3c item 1 - "it's Jane, jane@acme.com" from a new
phone) is recorded on the lead, but when the lead holds more than one confirmed
identifier of a kind, only the one this conversation's own transport carried is used
without asking; otherwise Luciel asks and offers the record's candidate MASKED.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Session
from app.models.lead import LeadIdentifier
from app.runtime.conversation_sender import is_session_sender
from app.runtime.lead_capture import identifier_kind, normalize_identifier, normalize_phone

#: outbound channel -> the identifier kind it needs
KIND_FOR_CHANNEL: dict[str, str] = {"email": "email", "sms": "phone"}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{6,}\d")

_REQUEST_EMAIL = re.compile(
    r"\b(e-?mail)\b.{0,24}\b(me|it|that|them|those|this|"
    r"the (details|info|quote|summary|list|prices?))\b"
    r"|\b(by|via|over|through|to my) e-?mail\b"
    r"|\bsend (it|that|them|those|this|the \w+) (to my )?e-?mail\b",
    re.IGNORECASE,
)
_REQUEST_SMS = re.compile(
    r"\btext (me|it|that|them|those|this|the \w+)\b"
    r"|\b(by|via|over|through) (text|sms)\b"
    r"|\bsend (me )?a text\b"
    r"|\bto my (phone|mobile|cell)\b",
    re.IGNORECASE,
)
_CONFIRM = re.compile(
    r"^\s*(yes|yep|yeah|yup|sure|correct|that'?s right|right|please do|go ahead|ok(ay)?|"
    r"confirm(ed)?|do it|please)\b",
    re.IGNORECASE,
)
_DECLINE = re.compile(
    r"^\s*(no|nope|nah|don'?t|do not|never ?mind|cancel|not now|skip|forget it)\b",
    re.IGNORECASE,
)


def detect_requested_channel(text: str) -> str | None:
    """'email me…' → email; 'text me…' → sms; else None."""
    t = text or ""
    if _REQUEST_EMAIL.search(t):
        return "email"
    if _REQUEST_SMS.search(t):
        return "sms"
    return None


def is_confirmation(text: str) -> bool:
    return bool(_CONFIRM.match(text or ""))


def is_decline(text: str) -> bool:
    return bool(_DECLINE.match(text or ""))


def identifier_for(kind: str, text: str) -> str | None:
    """The address/number of ``kind`` typed in ``text`` (normalised), or None."""
    if kind == "email":
        m = _EMAIL_RE.search(text or "")
        return normalize_identifier(m.group(0)) if m else None
    m = _PHONE_RE.search(text or "")
    return normalize_phone(m.group(0)) if m else None


@dataclass(frozen=True)
class PendingOutbound:
    """The answer parked on a session until the customer confirms where it goes."""

    channel: str
    body: str
    candidate: str | None
    asked_at: str

    def as_json(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: object) -> PendingOutbound | None:
        if not isinstance(raw, dict) or raw.get("channel") not in KIND_FOR_CHANNEL:
            return None
        return cls(
            channel=str(raw["channel"]),
            body=str(raw.get("body") or ""),
            candidate=(str(raw["candidate"]) if raw.get("candidate") else None),
            asked_at=str(raw.get("asked_at") or ""),
        )


def new_pending(channel: str, body: str, candidate: str | None) -> PendingOutbound:
    return PendingOutbound(
        channel=channel,
        body=body,
        candidate=candidate,
        asked_at=datetime.now(UTC).isoformat(),
    )


# --- copy (deterministic, never a real-time claim) --------------------------------------


def mask_identifier(value: str) -> str:
    """An address/number as it may be SHOWN to whoever is in the conversation: enough
    for the real owner to recognise it, never the whole identifier. The candidate an
    ask offers comes off the lead record, and the person asking may have reached that
    record by typing someone's email (§3.4.9: a typed identifier never gates a PII
    disclosure)."""
    text = value.strip()
    if "@" in text:
        local, _, domain = text.partition("@")
        return f"{local[:1]}***@{domain}"
    digits = re.sub(r"\D", "", text)
    return f"the number ending in {digits[-4:]}" if digits else "the number on file"


def ask_line(channel: str, candidate: str | None) -> str:
    noun = "email" if channel == "email" else "text"
    what = "email address" if channel == "email" else "mobile number"
    if candidate:
        return (
            f"Happy to {noun} you this. Should I send it to {mask_identifier(candidate)}? "
            "Reply yes to confirm."
        )
    return f"Happy to {noun} you this. What's the best {what} to send it to?"


def sent_line(channel: str, to: str) -> str:
    if channel == "email":
        return f"Sent — check your email at {to}."
    return f"Sent — check your messages at {to}."


def also_sent_line(channel: str, to: str) -> str:
    if channel == "email":
        return f"I've also emailed this to {to}."
    return f"I've also texted this to {to}."


def failed_line(channel: str) -> str:
    noun = "email" if channel == "email" else "text"
    return f"I couldn't {noun} that just now, so here it is in full."


DECLINED_LINE = "No problem — I'll keep everything here in this conversation."


# --- confirmed identifiers ---------------------------------------------------------------


async def confirmed_identifier(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    lead_id: uuid.UUID | None,
    kind: str,
    sess: Session | None = None,
    transport_identifier: str | None = None,
) -> str | None:
    """An identifier of ``kind`` the customer has CONFIRMED (transport-carried or
    confirmed in conversation) that THIS conversation may send to without asking.

    One confirmed identifier of the kind: that one. Several (a customer's second
    phone, or a number that joined the lead through a typed claim - WP-3c item 1):
    only the one this conversation's own transport carried (``transport_identifier``,
    else the session's persisted sender / participant key); none of them → None, so
    the caller ASKS instead of silently picking the newest. The newest used to win:
    a stranger who texted "it's Jane, jane@acme.com" became the address Jane's later
    "text me the quote" went to (phase-2 security review)."""
    if lead_id is None:
        return None
    values = [
        str(v)
        for v in (
            await session.execute(
                select(LeadIdentifier.value)
                .where(
                    LeadIdentifier.admin_id == admin_id,
                    LeadIdentifier.instance_id == instance_id,
                    LeadIdentifier.lead_id == lead_id,
                    LeadIdentifier.kind == kind,
                    LeadIdentifier.confirmed_at.is_not(None),
                )
                .order_by(LeadIdentifier.confirmed_at.desc(), LeadIdentifier.id)
            )
        ).scalars()
        if v
    ]
    if len(values) <= 1:
        return values[0] if values else None
    own = normalize_identifier(transport_identifier) if transport_identifier else None
    for value in values:
        if own is not None and normalize_identifier(value) == own:
            return value
        if sess is not None and is_session_sender(admin_id, sess, value):
            return value
    return None


#: The identifier kinds that double as a tool's required input of the same name
#: (``email`` for the scheduling tools, ``phone`` for a text).
_INPUT_KINDS = ("email", "phone")


async def confirmed_inputs_on_file(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    lead_id: uuid.UUID | None,
    transport_identifier: str | None = None,
) -> dict[str, str]:
    """The tool inputs this conversation can fill without asking (round 7 WP-2
    remainder, item 10): the lead's CONFIRMED email and phone — confirmed at capture
    when the transport carried them, or in conversation — and the transport
    identifier itself when it IS one of those kinds. Never an address that was merely
    mentioned: the same confirmation bar as sending to it. A lead holding several
    confirmed identifiers of a kind fills the one THIS conversation's transport
    carried, else nothing (``confirmed_identifier``)."""
    out: dict[str, str] = {}
    for kind in _INPUT_KINDS:
        value = await confirmed_identifier(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            lead_id=lead_id,
            kind=kind,
            transport_identifier=transport_identifier,
        )
        if value:
            out[kind] = value
    if transport_identifier:
        key = normalize_identifier(transport_identifier)
        kind = identifier_kind(key)
        if kind in _INPUT_KINDS:
            out.setdefault(kind, key)
    return out


async def confirm_identifier(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    lead_id: uuid.UUID,
    kind: str,
    value: str,
    source: str,
) -> None:
    """Record ``value`` as confirmed for the lead (``source``: transport | volunteered).
    Idempotent; a value already confirmed keeps its first confirmation."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: PLC0415

    now = datetime.now(UTC)
    await session.execute(
        pg_insert(LeadIdentifier)
        .values(
            id=uuid.uuid4(),
            admin_id=admin_id,
            instance_id=instance_id,
            lead_id=lead_id,
            kind=kind,
            value=value,
            confirmed_at=now,
            source=source,
        )
        .on_conflict_do_nothing(constraint="uq_lead_identifier")
    )
    await session.execute(
        update(LeadIdentifier)
        .where(
            LeadIdentifier.admin_id == admin_id,
            LeadIdentifier.instance_id == instance_id,
            LeadIdentifier.kind == kind,
            LeadIdentifier.value == value,
            LeadIdentifier.confirmed_at.is_(None),
        )
        .values(confirmed_at=now, source=source)
    )
