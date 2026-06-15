"""Session lifecycle/boundary (§3.4.8) + lead identity resolution (§3.4.9).

Sessions are deterministically bounded — no LLM judgment governs start/end. The
session key is (instance_id, participant_id, channel) where participant_id is the
resolved_lead_id. A new inbound after a session closes starts a new session (a new
budget unit) and receives the prior-session summary as context, not the full
transcript.

Identity resolution assigns each channel an identifier strength; cross-session
memory is retrievable only across sessions sharing the SAME strong / platform-
scoped-strong identifier (HARD RULE §3.4.9). A weak widget token never inherits
another lead's history; a matching identifier never authorizes PII disclosure.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Session

# Inactivity timeout by channel interaction class (§3.4.8) — platform constants.
INACTIVITY_TIMEOUT = {
    "widget": timedelta(minutes=30),
    "sms": timedelta(hours=4),
    "whatsapp": timedelta(hours=4),
    "instagram_messenger": timedelta(hours=4),
    "email": timedelta(hours=24),
    "voice": timedelta(minutes=30),  # post-call follow-up window
}


class IdentityStrength(str, enum.Enum):
    strong = "strong"
    platform_scoped_strong = "platform_scoped_strong"
    weak = "weak"


IDENTITY_STRENGTH = {
    "email": IdentityStrength.strong,
    "sms": IdentityStrength.strong,
    "voice": IdentityStrength.strong,
    "whatsapp": IdentityStrength.strong,
    "instagram_messenger": IdentityStrength.platform_scoped_strong,
    "widget": IdentityStrength.weak,
}


def can_carry_cross_session_memory(channel: str) -> bool:
    """Cross-session memory only for strong / platform-scoped-strong identifiers.
    A weak widget token never inherits history (HARD RULE §3.4.9)."""
    return IDENTITY_STRENGTH.get(channel, IdentityStrength.weak) in (
        IdentityStrength.strong,
        IdentityStrength.platform_scoped_strong,
    )


def _now() -> datetime:
    return datetime.now(UTC)


async def resolve_or_open_session(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    channel: str,
    resolved_lead_id: uuid.UUID | None,
) -> tuple[Session, bool]:
    """Return (session, is_new). Finds a live session for the key, else opens one.

    Liveness is determined deterministically by the channel inactivity timeout —
    a session whose last_message_at is older than the timeout is considered closed,
    and a new inbound opens a fresh session (a new budget unit, §3.4.8).
    """
    timeout = INACTIVITY_TIMEOUT.get(channel, timedelta(minutes=30))
    cutoff = _now() - timeout

    existing = None
    if resolved_lead_id is not None:
        existing = (
            await session.execute(
                select(Session)
                .where(
                    Session.admin_id == admin_id,
                    Session.instance_id == instance_id,
                    Session.channel == channel,
                    Session.resolved_lead_id == resolved_lead_id,
                )
                .order_by(Session.last_message_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    if existing is not None and existing.last_message_at >= cutoff:
        existing.last_message_at = _now()
        return existing, False

    new_session = Session(
        id=uuid.uuid4(),
        admin_id=admin_id,
        instance_id=instance_id,
        resolved_lead_id=resolved_lead_id,
        channel=channel,
        mode="ai",
        counted=False,
        started_at=_now(),
        last_message_at=_now(),
    )
    session.add(new_session)
    await session.flush()
    return new_session, True
