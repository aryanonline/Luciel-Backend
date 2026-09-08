"""Operating context for the model's system prompt (round 6 WP-F).

Platform-RESOLVED facts about this deployment, composed into a stanza under the
invariants (``input_safety.compose_operating_context_stanza``): the business's short
name, that Luciel is available around the clock, the channels a customer can reach
the business on right now, how the team is notified on a hand-off (so the model
never promises a real-time human), whether a person is reachable now (WP-E), and
that an unlisted capability does not exist. Never admin free text, so never wrapped
as untrusted.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.connections import gate
from app.models.instance import Instance
from app.notifications.escalation_dispatch import has_configured_escalation_contact
from app.runtime import team_availability as team_hours
from app.schemas_api.luciel import EscalationContactWire

CHANNEL_PHRASES: dict[str, str] = {
    "widget": "the website chat",
    "email": "email",
    "sms": "text message",
    "voice": "phone",
    "whatsapp": "WhatsApp",
    "messenger": "Messenger",
    "instagram": "Instagram",
}


@dataclass(frozen=True)
class OperatingContext:
    business_short_name: str | None
    live_channels: tuple[str, ...]
    escalation_channel: str  # email | sms
    escalation_configured: bool
    team_reachable_now: bool | None
    next_reachable_phrase: str | None


async def build_operating_context(
    session: AsyncSession,
    inst: Instance,
    *,
    admin_id: uuid.UUID,
    now: datetime | None = None,
) -> OperatingContext:
    """One connection-status read plus the instance already in hand — cheap enough
    for every turn."""
    statuses = await gate.channel_connection_statuses(
        session, admin_id=admin_id, instance_id=inst.id
    )
    live: list[str] = []
    for c in inst.channels or []:
        if not isinstance(c, dict) or not c.get("enabled"):
            continue
        channel = str(c["id"])
        if channel == "widget" or statuses.get(channel) == "connected":
            live.append(channel)
    cfg = EscalationContactWire.model_validate(dict(inst.escalation or {}))
    hours = team_hours.parse(inst.team_availability, inst.timezone)
    reachable: bool | None = None
    phrase: str | None = None
    if hours is not None:
        reachable = team_hours.team_reachable_now(hours, now)
        phrase = None if reachable else team_hours.describe_next_window(hours, now)
    return OperatingContext(
        business_short_name=(inst.business_short_name or "").strip() or None,
        live_channels=tuple(live),
        escalation_channel=cfg.preferred_channel or "email",
        escalation_configured=has_configured_escalation_contact(inst.escalation),
        team_reachable_now=reachable,
        next_reachable_phrase=phrase,
    )
