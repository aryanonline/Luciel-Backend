"""The carrier's word on a HUMAN take-over reply sent as a text (round 7 M10).

A person's reply on an SMS conversation (or a voice reply bridged to a text) used to
be stamped once, from Twilio's API accept, and never again: the delivery receipt the
carrier posts later updated ``escalation_events`` only (the owner pages), because the
transcript row did not say which message the receipt was about. A reply the carrier
then failed to deliver kept reading as sent.

The take-over route now asks Twilio for a receipt on these sends (the same tenant
status route the owner pages use) and keeps the MessageSid on the row
(``transcripts.delivery_provider_message_id``, migration 0076). When a verified
receipt names no page, this module looks for a ``human_agent`` row written under that
SID and records the carrier's word on it:

* ``delivered`` / ``read`` → ``delivered``, ``delivered_at`` = when the carrier said so;
* ``failed`` / ``undelivered`` → ``failed`` with ``sms_dlr:<status>[:<code>]`` as the
  stored detail (the wire collapses it to ``send_failed``) and no delivered time;
* interim statuses (queued, sent, …) change nothing.

A receipt is never applied to a row of another role, and a failure already recorded
is not overwritten by a later, retried receipt.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Transcript

_DLR_DELIVERED = frozenset({"delivered", "read"})
_DLR_FAILED = frozenset({"failed", "undelivered"})


async def record_reply_delivery_receipt(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    provider_message_id: str,
    message_status: str,
    error_code: str | None = None,
) -> str:
    """Stamp a human reply's row from a carrier receipt. Returns what was done:
    delivered | failed | ignored | unknown_message (no human reply carries the SID)."""
    status = (message_status or "").strip().lower()
    if status not in _DLR_DELIVERED and status not in _DLR_FAILED:
        return "ignored"
    row = (
        await session.execute(
            select(Transcript).where(
                Transcript.admin_id == admin_id,
                Transcript.delivery_provider_message_id == provider_message_id,
                Transcript.role == "human_agent",
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return "unknown_message"
    if row.delivery_status == "failed":
        return "ignored"  # settled by an earlier receipt; a retry changes nothing
    if status in _DLR_DELIVERED:
        row.delivery_status = "delivered"
        row.delivered_at = datetime.now(UTC)
        await session.flush()
        return "delivered"
    row.delivery_status = "failed"
    row.delivery_detail = (f"sms_dlr:{status}" + (f":{error_code}" if error_code else ""))[:64]
    row.delivered_at = None
    await session.flush()
    return "failed"
