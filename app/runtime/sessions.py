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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.models.audit import ActorType
from app.models.conversation import Session
from app.models.instance import Instance
from app.models.lead import Lead, LeadState

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

    Architecture §3.4.10a: if the resolved lead is in ``archived`` state, it is
    automatically reactivated (archived → active) and a ``lead_reactivated`` audit
    event is emitted.
    """
    timeout = INACTIVITY_TIMEOUT.get(channel, timedelta(minutes=30))
    cutoff = _now() - timeout

    # §3.4.10a: check if the resolved lead is archived and reactivate it.
    if resolved_lead_id is not None:
        await _maybe_reactivate_lead(session, admin_id=admin_id, lead_id=resolved_lead_id)

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
        # §3.7.1/§3.8.7 rule E: freeze the EFFECTIVE additive/neutral config at open so
        # later additive pillar edits apply to the NEXT conversation, not this one.
        config_snapshot=await _build_config_snapshot(
            session, admin_id=admin_id, instance_id=instance_id
        ),
    )
    session.add(new_session)
    await session.flush()
    return new_session, True


async def _build_config_snapshot(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
) -> dict[str, object] | None:
    """Snapshot the EFFECTIVE additive/neutral config at session-open (§3.8.7 rule E).

    Captures the CAPABILITY set — which tools/channels are enabled and the personality/
    business context — as it stood when the conversation began. Usability of each
    capability is NOT frozen here: connection health + disable/revoke stay LIVE via the
    broker gates (the safety half must still win). Returns None if the instance is
    missing so the caller stores NULL and the orchestrator falls back to live config.
    """
    inst: Instance | None = (
        await session.execute(
            select(Instance).where(Instance.admin_id == admin_id, Instance.id == instance_id)
        )
    ).scalar_one_or_none()
    if inst is None:
        return None
    return {
        "enabled_tools": [
            str(t["id"]) for t in inst.tools if isinstance(t, dict) and t.get("enabled")
        ],
        "enabled_channels": [
            str(c["id"]) for c in inst.channels if isinstance(c, dict) and c.get("enabled")
        ],
        "personality": dict(inst.personality),
        "business_context": inst.business_one_liner,
    }


@dataclass(frozen=True)
class EffectiveConfig:
    """The additive/neutral effective config a turn runs against (§3.8.7 rule E).

    ``snapshot_tool_ids`` is the frozen CAPABILITY set (None = older session with no
    snapshot → the orchestrator applies no snapshot filter and relies on the broker's
    live gates). ``business_context`` / ``enabled_channels`` are the neutral tuning
    captured at open (or live-config fallback when the snapshot is NULL)."""

    enabled_channels: set[str]
    business_context: str | None
    snapshot_tool_ids: set[str] | None


def effective_config(
    snapshot: dict[str, object] | None,
    instance: Instance,
    *,
    always_include_channel: str | None = None,
) -> EffectiveConfig:
    """Resolve the effective additive/neutral config for a turn (§3.8.7 rule E).

    Prefers the session's ``config_snapshot`` (frozen at open) so additive pillar edits
    made mid-conversation defer to the NEXT conversation. Falls back to the LIVE
    instance config when the snapshot is NULL (older sessions still work). Connection
    health + disable/revoke are NOT resolved here — they stay LIVE in the broker gates
    (capability=snapshot, usability=live: the safety half must still win)."""
    business_context: str | None
    if snapshot:
        snap_channels = cast("list[object]", snapshot.get("enabled_channels") or [])
        snap_tools = cast("list[object]", snapshot.get("enabled_tools") or [])
        channels = {str(ch) for ch in snap_channels}
        raw_ctx = snapshot.get("business_context")
        business_context = str(raw_ctx) if raw_ctx is not None else None
        tool_ids: set[str] | None = {str(t) for t in snap_tools}
    else:
        channels = {
            str(c["id"]) for c in instance.channels if isinstance(c, dict) and c.get("enabled")
        }
        business_context = instance.business_one_liner
        tool_ids = None  # no snapshot → rely on the broker's live gates only
    if always_include_channel is not None:
        channels = channels | {always_include_channel}
    return EffectiveConfig(
        enabled_channels=channels,
        business_context=business_context,
        snapshot_tool_ids=tool_ids,
    )


async def _maybe_reactivate_lead(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    lead_id: uuid.UUID,
) -> bool:
    """Reactivate an archived lead on inbound contact (Architecture §3.4.10a).

    If the lead exists and is in ``archived`` state, transition it to ``active``
    and emit ``lead_reactivated`` to the audit log. Returns True if reactivated."""
    lead: Lead | None = (
        await session.execute(select(Lead).where(Lead.admin_id == admin_id, Lead.id == lead_id))
    ).scalar_one_or_none()
    if lead is None or lead.state != LeadState.archived:
        return False

    lead.state = LeadState.active
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=lead.instance_id,
        actor_type=ActorType.platform,
        event_type="lead_reactivated",
        payload={"lead_id": str(lead_id)},
    )
    await session.flush()
    return True
