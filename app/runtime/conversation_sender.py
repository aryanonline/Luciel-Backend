"""Who is actually on the other end of THIS conversation (round 7 phase-2 hardening).

A lead can be known by several identifiers of one kind: two phones after a customer
wrote from a second number, or after WP-3c item 1 joined a new number to an existing
record on a typed claim. Anything that answers "where does a message for this
conversation go?" by reading the LEAD record picks whichever number the record
happens to be keyed on — not necessarily the one in the conversation. The owner's
takeover reply then reached a different phone and was stamped delivered.

The answer lives on the SESSION: the raw sender its transport carried, persisted at
ingress (``sessions.transport_sender``, migration 0074 — set by
``inbound_dispatch.persist_reply_context`` for sms/email and by the voice pipeline
from the caller's number). Sessions opened before that column existed are answered
from what they DO carry: the call record's caller number (voice), else the lead
identifier whose participant key equals the session's own (§3.4.8 — the key is
``sha256(admin ‖ channel ‖ transport identifier)``). Nothing matches → None: the
caller says so honestly, never "another identifier of the same kind".
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Session
from app.models.lead import Lead, LeadIdentifier
from app.runtime.lead_capture import identifier_kind, normalize_identifier
from app.runtime.sessions import participant_key_for

#: The channels whose session carries a raw sender this module can answer for, and
#: the identifier kind that sender is.
SENDER_KIND: dict[str, str] = {"sms": "phone", "voice": "phone", "email": "email"}

#: Column bound (migration 0074): an RFC 5321 path is at most 320 characters.
TRANSPORT_SENDER_MAX = 320


def clip_sender(value: object) -> str | None:
    """The raw sender as stored: stripped, bounded, None when empty."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:TRANSPORT_SENDER_MAX] if text else None


def is_session_sender(admin_id: uuid.UUID, sess: Session, value: str | None) -> bool:
    """Is ``value`` the identifier this session's own transport carried?

    The persisted sender when there is one; otherwise the participant-key match
    (sms/email sessions keyed on their sender; a voice session keyed on its CallSid
    never matches a number this way)."""
    if not value:
        return False
    key = normalize_identifier(value)
    if sess.transport_sender:
        return normalize_identifier(sess.transport_sender) == key
    if not sess.participant_key:
        return False
    return participant_key_for(admin_id, sess.channel, value) == sess.participant_key


async def conversation_sender(
    s: AsyncSession, *, admin_id: uuid.UUID, sess: Session, lead: Lead | None
) -> str | None:
    """The identifier a message for THIS conversation goes to on its own channel
    (sms / voice / email), or None when the conversation's sender cannot be told.

    Never a different identifier of the same kind from the lead record: a lead known
    by two phones answers with the one in the conversation or not at all."""
    kind = SENDER_KIND.get(sess.channel)
    if kind is None:
        return None
    if sess.transport_sender:
        return sess.transport_sender.strip()
    if sess.channel == "voice":
        # A legacy call session is keyed on the CallSid, which no number hashes to;
        # the call's own record still names the caller (scrubbed to "" on erasure).
        from app.models.voice import VoiceCallRecord  # noqa: PLC0415

        caller = (
            await s.execute(
                select(VoiceCallRecord.from_number)
                .where(
                    VoiceCallRecord.admin_id == admin_id,
                    VoiceCallRecord.session_id == sess.id,
                    VoiceCallRecord.from_number != "",
                )
                .order_by(VoiceCallRecord.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if caller:
            return str(caller).strip()
    if lead is None or not sess.participant_key:
        return None
    candidates: list[str] = [
        v for v in (lead.contact_identifier, lead.email, lead.phone) if v is not None
    ]
    candidates += [
        str(v)
        for v in (
            await s.execute(
                select(LeadIdentifier.value).where(
                    LeadIdentifier.admin_id == admin_id,
                    LeadIdentifier.lead_id == lead.id,
                    LeadIdentifier.kind == kind,
                )
            )
        ).scalars()
    ]
    for value in candidates:
        if identifier_kind(value.strip()) != kind:
            continue
        if participant_key_for(admin_id, sess.channel, value) == sess.participant_key:
            return value.strip()
    return None
