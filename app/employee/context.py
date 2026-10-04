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
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

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
    # Round 7 WP-2 remainder, item 11: the business's IANA zone (instances.timezone,
    # WP-E) when set and valid, and today's date on that clock (UTC's date when no
    # zone is set) — so the model reads a customer's "3pm Tuesday" on the business's
    # clock and says it back the same way, and the appointment tools
    # (ToolContext.timezone) do the same in code. A context built without them keeps
    # the prompt byte-identical; no zone keeps the tools on UTC, as before.
    timezone: str | None = None
    local_today: str | None = None


def business_timezone(name: str | None) -> str | None:
    """The instance's zone name when it is a real IANA zone, else None."""
    cleaned = (name or "").strip()
    return cleaned if cleaned and team_hours.valid_timezone(cleaned) else None


def local_today_phrase(zone: str | None, now: datetime | None) -> str:
    """'Tuesday 06 October 2026' on the business's clock — UTC's date without a zone."""
    moment = now if now is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(ZoneInfo(zone) if zone else UTC).strftime("%A %d %B %Y")


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
    zone = business_timezone(inst.timezone)
    return OperatingContext(
        business_short_name=(inst.business_short_name or "").strip() or None,
        live_channels=tuple(live),
        escalation_channel=cfg.preferred_channel or "email",
        escalation_configured=has_configured_escalation_contact(inst.escalation),
        team_reachable_now=reachable,
        next_reachable_phrase=phrase,
        timezone=zone,
        local_today=local_today_phrase(zone, now),
    )
