"""Employee status — what Luciel can do, needs, and did (round 6 WP-F).

Two halves, deliberately split:

* ``Snapshot`` is every fact the rules read, gathered ONCE by
  ``build_employee_status`` from rows that already exist (instance config, live
  connection statuses, knowledge sources, contact health, the metered budget, leads,
  deferred actions, and the day's sessions/escalations/bookings).
* ``derive_capabilities`` / ``derive_needs`` are pure functions over that snapshot —
  one rule each, pinned without a database — so the Today card, the morning brief
  and the tests all read the same verdicts.

Principles: Luciel is on duty around the clock (``ON_DUTY`` is a constant, never a
computed state); a capability is ``ready`` only when its connection is live; the
owner's "team availability" describes people, never Luciel; "what I did" is counts
and links, never a model's narration.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing import metering
from app.connections import gate, registry
from app.models.audit import AdminAuditLog
from app.models.booking import BookingEvent
from app.models.conversation import EscalationEvent
from app.models.conversation import Session as SessionModel
from app.models.deferred import DeferredAction
from app.models.instance import Instance
from app.models.knowledge import KnowledgeSource
from app.models.lead import Lead
from app.notifications.escalation_dispatch import has_configured_escalation_contact
from app.runtime import team_availability as team_hours
from app.tools import catalog

#: The one thing that is never derived: Luciel has no hours.
ON_DUTY = "around the clock"

#: Free-cap "approaching" threshold the overview already uses (Customer Journey §6).
NEAR_CAP_AT = 40
#: A synced source older than this is "stale" (the knowledge_sync sweep runs daily).
STALE_SYNC_AFTER = timedelta(days=7)
#: Abandoned callbacks are worth a line for this long.
RECENT_ABANDON_WINDOW = timedelta(days=7)

CHANNEL_LABELS: dict[str, str] = {
    "widget": "Website chat",
    "email": "Email",
    "sms": "Text messages",
    "voice": "Phone calls",
    "whatsapp": "WhatsApp",
    "messenger": "Messenger",
    "instagram": "Instagram",
}

#: Statuses that mean "nothing is connected" (as opposed to "connected but broken").
_MISSING = frozenset({"unconfigured", "not_connected", "revoked"})
_STATUS_DETAIL: dict[str, str] = {
    "expired": "needs reconnecting",
    "error": "connection error — reconnect",
    "dormant": "dormant — reconnect",
    "pending_carrier_registration": "carrier registration pending",
    "pending_email_routing": "email routing pending",
    "not_operable_hosting_required": "number is not in your Twilio account",
}


@dataclass(frozen=True)
class Capability:
    id: str
    kind: str  # channel | tool | knowledge | escalation | budget
    label: str
    state: str  # ready | attention | off | unavailable
    detail: str | None = None


@dataclass(frozen=True)
class Need:
    code: str
    severity: str  # attention | info
    title: str
    detail: str
    href: str


@dataclass(frozen=True)
class DayCounts:
    date: str
    conversations: int = 0
    by_channel: dict[str, int] = field(default_factory=dict)
    leads: int = 0
    leads_to_crm: int = 0
    escalations: int = 0
    escalations_reached: int = 0
    bookings: int = 0
    callbacks_scheduled: int = 0
    human_takeovers: int = 0

    @property
    def quiet(self) -> bool:
        return not any(
            (
                self.conversations,
                self.leads,
                self.escalations,
                self.bookings,
                self.callbacks_scheduled,
                self.human_takeovers,
            )
        )


@dataclass(frozen=True)
class Snapshot:
    """Every fact the rules read, gathered once."""

    assistant_name: str
    business_short_name: str | None
    luciel_state: str
    channels_enabled: dict[str, bool]
    channel_status: dict[str, str]  # wire status for connection-backed channels
    tools_enabled: dict[str, bool]
    tool_status: dict[str, str | None]  # raw status for connection-backed tools
    tool_provider_available: dict[str, bool]
    knowledge_sources: int
    knowledge_errors: int
    knowledge_oldest_sync: datetime | None
    escalation_configured: bool
    escalation_bouncing: bool
    escalation_unconfirmed: bool
    conversations_used: int
    free_allowance: int
    billed_blocks: int
    billable: bool
    at_cap: bool
    near_next_block: bool
    billing_state: str
    dunning_state: str
    period_resets_at: datetime
    leads_failed_crm: int
    callbacks_abandoned_recent: int
    team_availability_set: bool
    team_reachable_now: bool | None
    next_reachable: str | None
    timezone: str | None
    allowed_origins_set: bool
    daily_brief_enabled: bool
    now: datetime


@dataclass(frozen=True)
class EmployeeStatus:
    assistant_name: str
    business_short_name: str | None
    on_duty: str
    conversations_used: int
    free_allowance: int
    free_remaining: int
    billed_blocks: int
    billable: bool
    period_resets_at: datetime
    timezone: str | None
    team_reachable_now: bool | None
    next_reachable: str | None
    capabilities: list[Capability]
    needs: list[Need]
    yesterday: DayCounts
    today: DayCounts
    daily_brief_enabled: bool


# --- pure rules ---------------------------------------------------------------------


def _tool_label(tool_id: str) -> str:
    return catalog.TOOL_DISPLAY_NAMES.get(tool_id, tool_id.replace("_", " ").capitalize())


def derive_capabilities(snap: Snapshot) -> list[Capability]:
    out: list[Capability] = []
    for channel, enabled in snap.channels_enabled.items():
        label = CHANNEL_LABELS.get(channel, channel)
        if not enabled:
            out.append(Capability(channel, "channel", label, "off"))
            continue
        if channel == "widget":
            out.append(Capability(channel, "channel", label, "ready", "on your website"))
            continue
        status = snap.channel_status.get(channel, "unconfigured")
        if status == "connected":
            out.append(Capability(channel, "channel", label, "ready"))
        elif status in _MISSING:
            out.append(Capability(channel, "channel", label, "attention", "nothing connected"))
        else:
            out.append(
                Capability(
                    channel, "channel", label, "attention", _STATUS_DETAIL.get(status, status)
                )
            )
    for tool_id, enabled in snap.tools_enabled.items():
        label = _tool_label(tool_id)
        if not enabled:
            out.append(Capability(tool_id, "tool", label, "off"))
            continue
        if tool_id not in snap.tool_status:
            out.append(Capability(tool_id, "tool", label, "ready"))
            continue
        if not snap.tool_provider_available.get(tool_id, True):
            out.append(Capability(tool_id, "tool", label, "unavailable", "not available yet"))
            continue
        tool_state = snap.tool_status[tool_id]
        if tool_state == "connected":
            out.append(Capability(tool_id, "tool", label, "ready"))
        elif tool_state is None or tool_state in _MISSING:
            out.append(Capability(tool_id, "tool", label, "attention", "nothing connected"))
        else:
            out.append(
                Capability(
                    tool_id, "tool", label, "attention", _STATUS_DETAIL.get(tool_state, tool_state)
                )
            )
    # Knowledge
    if snap.knowledge_sources == 0:
        out.append(Capability("knowledge", "knowledge", "Knowledge", "attention", "nothing yet"))
    elif snap.knowledge_errors:
        out.append(
            Capability(
                "knowledge",
                "knowledge",
                "Knowledge",
                "attention",
                f"{snap.knowledge_errors} source(s) failing to sync",
            )
        )
    elif _stale(snap):
        out.append(
            Capability("knowledge", "knowledge", "Knowledge", "attention", "not synced lately")
        )
    else:
        out.append(
            Capability(
                "knowledge",
                "knowledge",
                "Knowledge",
                "ready",
                f"{snap.knowledge_sources} source(s)",
            )
        )
    # Escalation
    if not snap.escalation_configured:
        out.append(
            Capability("escalation", "escalation", "Escalation", "attention", "no contact set")
        )
    elif snap.escalation_bouncing:
        out.append(
            Capability("escalation", "escalation", "Escalation", "attention", "contact is bouncing")
        )
    elif snap.escalation_unconfirmed:
        out.append(
            Capability(
                "escalation", "escalation", "Escalation", "attention", "contact not confirmed"
            )
        )
    else:
        out.append(Capability("escalation", "escalation", "Escalation", "ready"))
    # Budget
    if snap.at_cap:
        out.append(Capability("budget", "budget", "Conversations", "attention", "at capacity"))
    else:
        used = f"{snap.conversations_used} of {snap.free_allowance} free used"
        detail = used if snap.billed_blocks == 0 else f"{used}; {snap.billed_blocks} paid block(s)"
        out.append(Capability("budget", "budget", "Conversations", "ready", detail))
    return out


def _stale(snap: Snapshot) -> bool:
    return (
        snap.knowledge_oldest_sync is not None
        and snap.now - snap.knowledge_oldest_sync > STALE_SYNC_AFTER
    )


def derive_needs(snap: Snapshot) -> list[Need]:  # noqa: C901, PLR0912, PLR0915 — one rule each
    needs: list[Need] = []
    configure = "/dashboard/configure"
    if snap.luciel_state == "paused":
        needs.append(
            Need(
                "paused",
                "attention",
                "Luciel is paused",
                "Customers on every channel get no replies until you resume.",
                "/dashboard",
            )
        )
    for channel, enabled in snap.channels_enabled.items():
        if not enabled or channel == "widget":
            continue
        label = CHANNEL_LABELS.get(channel, channel)
        status = snap.channel_status.get(channel, "unconfigured")
        if status == "connected":
            continue
        if channel == "email" and status in _MISSING:
            needs.append(
                Need(
                    "email_no_mailbox",
                    "attention",
                    "Email channel is on but no mailbox is connected",
                    "Connect your work mailbox under Configure → Channels, or turn email off.",
                    configure,
                )
            )
        elif status in _MISSING:
            needs.append(
                Need(
                    f"channel_disconnected:{channel}",
                    "attention",
                    f"{label} is on but nothing is connected",
                    "Connect the account under Configure → Channels, or turn the channel off.",
                    configure,
                )
            )
        else:
            needs.append(
                Need(
                    f"channel_attention:{channel}",
                    "attention",
                    f"{label}: {_STATUS_DETAIL.get(status, status)}",
                    "Open Configure → Channels for the exact step.",
                    configure,
                )
            )
    for tool_id, enabled in snap.tools_enabled.items():
        if not enabled or tool_id not in snap.tool_status:
            continue
        label = _tool_label(tool_id)
        if not snap.tool_provider_available.get(tool_id, True):
            needs.append(
                Need(
                    f"tool_unavailable:{tool_id}",
                    "info",
                    f"{label} isn't available yet",
                    "Luciel tells customers it can't do this when asked. Turn it off to hide it.",
                    configure,
                )
            )
            continue
        tool_state = snap.tool_status[tool_id]
        if tool_state == "connected":
            continue
        if tool_state is None or tool_state in _MISSING:
            needs.append(
                Need(
                    f"tool_disconnected:{tool_id}",
                    "attention",
                    f"{label} is on but nothing is connected",
                    "Connect the account under Configure → Tools, or turn the tool off.",
                    configure,
                )
            )
        else:
            needs.append(
                Need(
                    f"tool_attention:{tool_id}",
                    "attention",
                    f"{label}: {_STATUS_DETAIL.get(tool_state, tool_state)}",
                    "Open Configure → Tools for the exact step.",
                    configure,
                )
            )
    if not snap.escalation_configured:
        needs.append(
            Need(
                "no_escalation_contact",
                "attention",
                "No escalation contact",
                "Hand-offs only reach your account email. Add a contact under "
                "Configure → Escalation.",
                configure,
            )
        )
    elif snap.escalation_bouncing:
        needs.append(
            Need(
                "escalation_bouncing",
                "attention",
                "Your escalation contact is bouncing",
                "Mail to it is failing; escalations fall back to your account email. "
                "Fix the address.",
                configure,
            )
        )
    elif snap.escalation_unconfirmed:
        needs.append(
            Need(
                "escalation_unconfirmed",
                "info",
                "Confirm your escalation contact",
                "Until the address is confirmed, the account owner is copied on every hand-off.",
                configure,
            )
        )
    if snap.knowledge_sources == 0:
        needs.append(
            Need(
                "knowledge_empty",
                "attention",
                "Luciel has nothing to answer from yet",
                "Add your website, files or a connected drive under Configure → Knowledge.",
                configure,
            )
        )
    elif snap.knowledge_errors:
        needs.append(
            Need(
                "knowledge_sync_error",
                "attention",
                f"{snap.knowledge_errors} knowledge source(s) failing to sync",
                "Reconnect or re-sync them under Configure → Knowledge.",
                configure,
            )
        )
    elif _stale(snap):
        needs.append(
            Need(
                "knowledge_stale",
                "info",
                "Knowledge hasn't synced in over a week",
                "Re-sync under Configure → Knowledge if the source still exists.",
                configure,
            )
        )
    if snap.at_cap:
        needs.append(
            Need(
                "at_cap",
                "attention",
                f"Out of free conversations ({snap.free_allowance} used this period)",
                "Luciel is still capturing leads and escalating, but replying at capacity. "
                "Add a card to keep it fully answering.",
                "/dashboard/billing",
            )
        )
    elif snap.billing_state == "free_cap" and snap.conversations_used >= NEAR_CAP_AT:
        needs.append(
            Need(
                "near_cap",
                "info",
                f"{snap.conversations_used} of {snap.free_allowance} free conversations used",
                "Add a card before the free ones run out; nothing changes until they do.",
                "/dashboard/billing",
            )
        )
    elif snap.near_next_block:
        needs.append(
            Need(
                "near_next_block",
                "info",
                "About 80% through the current paid block",
                "Just a heads-up — Luciel keeps answering; the next block starts when this "
                "one fills.",
                "/dashboard/billing",
            )
        )
    if snap.dunning_state in ("retrying", "reduced"):
        needs.append(
            Need(
                "payment_failed",
                "attention",
                "A payment failed",
                "Update the card on the Billing page so usage past the free conversations "
                "keeps being covered.",
                "/dashboard/billing",
            )
        )
    elif not snap.billable and snap.billed_blocks > 0:
        needs.append(
            Need(
                "card_removed",
                "attention",
                "No card on file for usage past the free conversations",
                "Paid blocks are accruing but cannot be charged. Add a card on the Billing page.",
                "/dashboard/billing",
            )
        )
    if snap.leads_failed_crm:
        needs.append(
            Need(
                "leads_not_in_crm",
                "attention",
                f"{snap.leads_failed_crm} lead(s) did not reach your CRM",
                "Review and retry them on the Leads page.",
                "/dashboard/leads",
            )
        )
    if snap.callbacks_abandoned_recent:
        needs.append(
            Need(
                "callbacks_abandoned",
                "info",
                f"{snap.callbacks_abandoned_recent} promised follow-up(s) could not be delivered",
                "See the conversation for who was waiting and reach out yourself.",
                "/dashboard/conversations",
            )
        )
    if not snap.team_availability_set:
        needs.append(
            Need(
                "team_availability_unset",
                "info",
                "Tell customers when your team is reachable (optional)",
                "Luciel answers around the clock either way; this only makes follow-up "
                "promises exact.",
                configure,
            )
        )
    if not snap.allowed_origins_set:
        needs.append(
            Need(
                "allowed_origins_unset",
                "info",
                "Name the websites that may load your chat",
                "Right now any page that copies your embed key can use it.",
                "/dashboard/embed",
            )
        )
    needs.sort(key=lambda n: 0 if n.severity == "attention" else 1)
    return needs


# --- gathering -------------------------------------------------------------------


def _tz(inst: Instance) -> tzinfo:
    if inst.timezone and team_hours.valid_timezone(inst.timezone):
        return ZoneInfo(inst.timezone)
    return UTC


def local_day_bounds(inst: Instance, now: datetime) -> tuple[datetime, datetime, datetime]:
    """(yesterday_start, today_start, now) in the owner's zone."""
    tz = _tz(inst)
    local_now = now.astimezone(tz)
    today_start = datetime.combine(local_now.date(), time.min, tzinfo=tz)
    return today_start - timedelta(days=1), today_start, local_now


async def day_counts(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    start: datetime,
    end: datetime,
) -> DayCounts:
    """Counts over [start, end) from rows that already exist — no narration."""
    by_channel_rows = (
        await session.execute(
            select(SessionModel.channel, func.count())
            .where(
                SessionModel.admin_id == admin_id,
                SessionModel.instance_id == instance_id,
                SessionModel.counted.is_(True),
                SessionModel.started_at >= start,
                SessionModel.started_at < end,
            )
            .group_by(SessionModel.channel)
        )
    ).all()
    by_channel = {str(ch): int(n) for ch, n in by_channel_rows}
    leads = (
        await session.execute(
            select(func.count()).where(
                Lead.admin_id == admin_id,
                Lead.instance_id == instance_id,
                Lead.created_at >= start,
                Lead.created_at < end,
            )
        )
    ).scalar_one()
    leads_to_crm = (
        await session.execute(
            select(func.count()).where(
                Lead.admin_id == admin_id,
                Lead.instance_id == instance_id,
                Lead.crm_push_status == "synced",
                Lead.crm_pushed_at >= start,
                Lead.crm_pushed_at < end,
            )
        )
    ).scalar_one()
    esc = (
        await session.execute(
            select(
                func.count(),
                func.sum(case((EscalationEvent.delivery_status == "delivered", 1), else_=0)),
            ).where(
                EscalationEvent.admin_id == admin_id,
                EscalationEvent.instance_id == instance_id,
                EscalationEvent.fired_at >= start,
                EscalationEvent.fired_at < end,
            )
        )
    ).one()
    bookings = (
        await session.execute(
            select(func.count()).where(
                BookingEvent.admin_id == admin_id,
                BookingEvent.instance_id == instance_id,
                BookingEvent.booked_at >= start,
                BookingEvent.booked_at < end,
            )
        )
    ).scalar_one()
    callbacks = (
        await session.execute(
            select(func.count()).where(
                DeferredAction.admin_id == admin_id,
                DeferredAction.instance_id == instance_id,
                DeferredAction.action_type == "schedule_callback",
                DeferredAction.created_at >= start,
                DeferredAction.created_at < end,
            )
        )
    ).scalar_one()
    takeovers = (
        await session.execute(
            select(func.count()).where(
                AdminAuditLog.admin_id == admin_id,
                AdminAuditLog.instance_id == instance_id,
                AdminAuditLog.event_type == "human_takeover_started",
                AdminAuditLog.created_at >= start,
                AdminAuditLog.created_at < end,
            )
        )
    ).scalar_one()
    return DayCounts(
        date=start.date().isoformat(),
        conversations=sum(by_channel.values()),
        by_channel=by_channel,
        leads=int(leads),
        leads_to_crm=int(leads_to_crm),
        escalations=int(esc[0] or 0),
        escalations_reached=int(esc[1] or 0),
        bookings=int(bookings),
        callbacks_scheduled=int(callbacks),
        human_takeovers=int(takeovers),
    )


def _provider_available(connection_type: str) -> bool:
    return any(
        registry.provider_available(spec.provider, connection_type)
        for spec in registry.providers_for(connection_type)
    )


async def gather_snapshot(
    session: AsyncSession,
    inst: Instance,
    *,
    admin_id: uuid.UUID,
    now: datetime,
) -> Snapshot:
    from app.models.email_incident import is_suppressed, is_verified  # noqa: PLC0415
    from app.notifications.delivery_incidents import states_for  # noqa: PLC0415

    channels_enabled = {
        str(c["id"]): bool(c.get("enabled")) for c in inst.channels if isinstance(c, dict)
    }
    channel_status = await gate.channel_connection_statuses(
        session, admin_id=admin_id, instance_id=inst.id
    )
    tools_enabled = {
        str(t["id"]): bool(t.get("enabled")) for t in inst.tools if isinstance(t, dict)
    }
    tool_status: dict[str, str | None] = {}
    tool_provider_available: dict[str, bool] = {}
    status_by_type: dict[str, str | None] = {}
    for tool_id in tools_enabled:
        connection_type = catalog.TOOL_CONNECTION_TYPE.get(tool_id)
        if not connection_type:
            continue
        if connection_type not in status_by_type:
            status_by_type[connection_type] = await gate.connection_status(
                session, admin_id=admin_id, instance_id=inst.id, connection_type=connection_type
            )
        tool_status[tool_id] = status_by_type[connection_type]
        tool_provider_available[tool_id] = _provider_available(connection_type)

    k_count, k_oldest, k_errors = (
        await session.execute(
            select(
                func.count(),
                func.min(KnowledgeSource.last_synced_at),
                func.sum(
                    case(
                        (KnowledgeSource.sync_status.in_(("error", "paused_reconnect_needed")), 1),
                        else_=0,
                    )
                ),
            ).where(
                KnowledgeSource.admin_id == admin_id,
                KnowledgeSource.instance_id == inst.id,
                KnowledgeSource.soft_deleted_at.is_(None),
            )
        )
    ).one()

    escalation = inst.escalation or {}
    configured = has_configured_escalation_contact(escalation)
    bouncing = unconfirmed = False
    emails = [
        str(a) for a in (escalation.get("primaryEmail"), escalation.get("secondaryEmail")) if a
    ]
    if emails:
        states = await states_for(session, admin_id, emails)
        rows = [states.get(a.strip().lower()) for a in emails]
        bouncing = any(is_suppressed(r) for r in rows)
        unconfirmed = not bouncing and not any(is_verified(r) for r in rows)

    budget = await metering.build_budget(session, admin_id=admin_id, instance_id=inst.id, now=now)

    leads_failed = (
        await session.execute(
            select(func.count()).where(
                Lead.admin_id == admin_id,
                Lead.instance_id == inst.id,
                Lead.crm_push_status == "failed",
            )
        )
    ).scalar_one()
    abandoned = (
        await session.execute(
            select(func.count()).where(
                DeferredAction.admin_id == admin_id,
                DeferredAction.instance_id == inst.id,
                DeferredAction.action_type == "schedule_callback",
                DeferredAction.status == "abandoned",
                DeferredAction.created_at >= now - RECENT_ABANDON_WINDOW,
            )
        )
    ).scalar_one()

    hours = team_hours.parse(inst.team_availability, inst.timezone)
    reachable: bool | None = None
    next_phrase: str | None = None
    if hours is not None:
        reachable = team_hours.team_reachable_now(hours, now)
        next_phrase = None if reachable else team_hours.describe_next_window(hours, now)

    personality = inst.personality or {}
    return Snapshot(
        assistant_name=str(personality.get("assistantName") or inst.name),
        business_short_name=(inst.business_short_name or "").strip() or None,
        luciel_state=inst.state.value,
        channels_enabled=channels_enabled,
        channel_status=channel_status,
        tools_enabled=tools_enabled,
        tool_status=tool_status,
        tool_provider_available=tool_provider_available,
        knowledge_sources=int(k_count or 0),
        knowledge_errors=int(k_errors or 0),
        knowledge_oldest_sync=k_oldest,
        escalation_configured=configured,
        escalation_bouncing=bouncing,
        escalation_unconfirmed=unconfirmed,
        conversations_used=budget.conversations_this_period,
        free_allowance=budget.free_allowance,
        billed_blocks=budget.billed_blocks,
        billable=budget.billable,
        at_cap=budget.at_cap,
        near_next_block=budget.near_next_block,
        billing_state=budget.billing_state,
        dunning_state=budget.dunning_state,
        period_resets_at=budget.period_resets_at,
        leads_failed_crm=int(leads_failed),
        callbacks_abandoned_recent=int(abandoned),
        team_availability_set=hours is not None,
        team_reachable_now=reachable,
        next_reachable=next_phrase,
        timezone=inst.timezone,
        allowed_origins_set=bool(inst.allowed_origins),
        daily_brief_enabled=bool(inst.daily_brief_enabled),
        now=now,
    )


async def build_employee_status(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    now: datetime | None = None,
) -> EmployeeStatus | None:
    """Everything the Today card / brief / prompt need, from rows that already exist."""
    moment = now or datetime.now(UTC)
    inst = (
        await session.execute(
            select(Instance).where(Instance.admin_id == admin_id, Instance.id == instance_id)
        )
    ).scalar_one_or_none()
    if inst is None:
        return None
    snap = await gather_snapshot(session, inst, admin_id=admin_id, now=moment)
    yesterday_start, today_start, local_now = local_day_bounds(inst, moment)
    yesterday = await day_counts(
        session, admin_id=admin_id, instance_id=inst.id, start=yesterday_start, end=today_start
    )
    today = await day_counts(
        session, admin_id=admin_id, instance_id=inst.id, start=today_start, end=local_now
    )
    return EmployeeStatus(
        assistant_name=snap.assistant_name,
        business_short_name=snap.business_short_name,
        on_duty=ON_DUTY,
        conversations_used=snap.conversations_used,
        free_allowance=snap.free_allowance,
        free_remaining=max(snap.free_allowance - snap.conversations_used, 0),
        billed_blocks=snap.billed_blocks,
        billable=snap.billable,
        period_resets_at=snap.period_resets_at,
        timezone=snap.timezone,
        team_reachable_now=snap.team_reachable_now,
        next_reachable=snap.next_reachable,
        capabilities=derive_capabilities(snap),
        needs=derive_needs(snap),
        yesterday=yesterday,
        today=today,
        daily_brief_enabled=snap.daily_brief_enabled,
    )
