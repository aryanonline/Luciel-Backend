"""Built-in tool catalog (Architecture §3.3.2) — the 8 admin-selectable tools.

Each tool declares its required connection_type, channels, execution mode, and
required inputs, and implements the five-outcome result contract (§3.3.4/§3.3.6).
External effects (calendar, CRM, record source, send) are reached through an
injected backend so the per-tool accuracy behavior is testable without real
providers; the real backends bind via the Connections layer (Arc 17).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import func, select

from app.connections import deferred_queue
from app.models.deferred import DeferredAction
from app.models.lead import Lead
from app.tools import contract as c
from app.tools.contract import Outcome, Tool, ToolContext, ToolResult

# --- schedule_callback rate-limit caps (Architecture §3.3.5 / Decision #51) ---- #
# schedule_callback is the one tool that creates FUTURE outbound sends, so it is
# rate-limited on two axes AT SCHEDULING TIME (before any DeferredAction row is
# written). These are PLATFORM CONSTANTS — deliberately NOT admin-configurable — so a
# tenant (or a misbehaving loop) cannot nag a lead or fan out unbounded future sends.
# Tunable here as platform policy only.
SCHEDULE_CALLBACK_PER_LEAD_PENDING_CAP = 3
SCHEDULE_CALLBACK_PER_LEAD_24H_CAP = 5
SCHEDULE_CALLBACK_PER_TENANT_PENDING_CAP = 50
SCHEDULE_CALLBACK_PER_TENANT_24H_CAP = 200
_ROLLING_WINDOW = timedelta(hours=24)


# --- backends (injected; real impls land in Arc 17) ------------------------- #
class CalendarBackend(Protocol):
    async def free_busy(self, ctx: ToolContext, window: str) -> list[str]: ...
    async def book(self, ctx: ToolContext, slot: str, email: str) -> dict[str, Any] | None: ...
    # Reschedule/cancel are READ-BEFORE-WRITE (§3.3.6): find the existing booking first,
    # then act on the reference it returns. Never guess which booking the caller meant.
    async def find_bookings(
        self, ctx: ToolContext, *, email: str, booking_ref: str | None = None
    ) -> list[dict[str, Any]]: ...
    async def reschedule(
        self, ctx: ToolContext, booking_ref: str, slot: str
    ) -> dict[str, Any] | None: ...
    async def cancel(self, ctx: ToolContext, booking_ref: str) -> dict[str, Any] | None: ...


class RecordBackend(Protocol):
    async def lookup(self, ctx: ToolContext, query: str) -> list[dict[str, Any]]: ...


class MessengerBackend(Protocol):
    async def send(self, ctx: ToolContext, to: str, body: str) -> str | None: ...


class CrmBackend(Protocol):
    async def upsert_lead(
        self, ctx: ToolContext, lead_key: str, fields: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Idempotent upsert maintaining the SAME lead's record (§3.3.6).

        Returns ``{"crm_record_id": ..., "action": "created"|"updated"|"delivered"}``
        on success (the create-vs-update history is traced, §3.3.6), or None for a
        provider field-mapping rejection. Raises on connection/provider failure.
        """
        ...


# --- tools ------------------------------------------------------------------ #
class CheckAvailabilityTool:
    tool_id = "check_availability"
    display_name = "Check availability"
    description = "Read the connected calendar's free/busy to find open slots."
    requires_connection: str | None = "calendar"
    requires_channels: frozenset[str] = frozenset()
    execution_mode = "in_process"
    required_inputs: frozenset[str] = frozenset({"window"})

    def __init__(self, backend: CalendarBackend) -> None:
        self.backend = backend

    async def execute(self, inputs: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            slots = await self.backend.free_busy(context, inputs["window"])
        except Exception as exc:  # noqa: BLE001
            return c.failed(f"calendar_error: {type(exc).__name__}")
        if not slots:
            return c.empty({"window": inputs["window"]})
        return c.ok({"slots": slots})


class BookAppointmentTool:
    tool_id = "book_appointment"
    display_name = "Book an appointment"
    description = "Write a booking into the connected calendar after availability is confirmed."
    requires_connection: str | None = "calendar"
    requires_channels: frozenset[str] = frozenset()
    execution_mode = "in_process"
    required_inputs: frozenset[str] = frozenset({"slot", "email"})

    def __init__(self, backend: CalendarBackend) -> None:
        self.backend = backend

    async def execute(self, inputs: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            booking = await self.backend.book(context, inputs["slot"], inputs["email"])
        except Exception as exc:  # noqa: BLE001
            return c.failed(f"calendar_error: {type(exc).__name__}")
        if booking is None:
            # slot taken between read and write — conflict (§3.3.6); never claim success.
            return c.failed("slot_conflict")
        return c.ok(booking)  # carries confirmed time + booking reference


_BOOKING_REF_KEYS = ("bookingRef", "booking_ref", "id")


def _booking_ref(booking: dict[str, Any]) -> str | None:
    for key in _BOOKING_REF_KEYS:
        value = booking.get(key)
        if value:
            return str(value)
    return None


async def _resolve_booking_ref(
    backend: CalendarBackend, inputs: dict[str, Any], context: ToolContext
) -> tuple[str | None, ToolResult | None]:
    """READ half of read-before-write (§3.3.6): find the ONE booking this turn means.

    Returns ``(booking_ref, None)`` on a single confident match, else ``(None, result)``
    with the outcome to return verbatim. Rescheduling or cancelling the wrong appointment
    is worse than asking, so a multi-match is ``ambiguous`` — the conversation loop asks
    which one rather than the tool picking the first."""
    ref = inputs.get("booking_ref") or None
    try:
        matches = await backend.find_bookings(
            context, email=inputs["email"], booking_ref=str(ref) if ref else None
        )
    except Exception as exc:  # noqa: BLE001
        return None, c.failed(f"calendar_error: {type(exc).__name__}")
    if not matches:
        return None, c.empty({"email": inputs["email"]})
    if len(matches) > 1:
        return None, c.ambiguous(matches)
    resolved = _booking_ref(matches[0])
    if resolved is None:
        # A match we cannot address is not a match we may write against.
        return None, c.missing_input(["booking_ref"])
    return resolved, None


class RescheduleAppointmentTool:
    tool_id = "reschedule_appointment"
    display_name = "Reschedule an appointment"
    description = "Move an existing booking on the connected calendar to a new slot."
    requires_connection: str | None = "calendar"
    requires_channels: frozenset[str] = frozenset()
    execution_mode = "in_process"
    # booking_ref is OPTIONAL: supplied it disambiguates, absent the tool reads first.
    required_inputs: frozenset[str] = frozenset({"email", "slot"})

    def __init__(self, backend: CalendarBackend) -> None:
        self.backend = backend

    async def execute(self, inputs: dict[str, Any], context: ToolContext) -> ToolResult:
        ref, early = await _resolve_booking_ref(self.backend, inputs, context)
        if ref is None:
            return early if early is not None else c.failed("booking_unresolved")
        try:
            moved = await self.backend.reschedule(context, ref, inputs["slot"])
        except Exception as exc:  # noqa: BLE001
            return c.failed(f"calendar_error: {type(exc).__name__}")
        if moved is None:
            # New slot taken between the read and the write — same conflict discipline as
            # book_appointment: never claim a move that did not happen (§3.3.6).
            return c.failed("slot_conflict")
        return c.ok(moved)


class CancelAppointmentTool:
    tool_id = "cancel_appointment"
    display_name = "Cancel an appointment"
    description = "Cancel an existing booking on the connected calendar."
    requires_connection: str | None = "calendar"
    requires_channels: frozenset[str] = frozenset()
    execution_mode = "in_process"
    required_inputs: frozenset[str] = frozenset({"email"})

    def __init__(self, backend: CalendarBackend) -> None:
        self.backend = backend

    async def execute(self, inputs: dict[str, Any], context: ToolContext) -> ToolResult:
        ref, early = await _resolve_booking_ref(self.backend, inputs, context)
        if ref is None:
            return early if early is not None else c.failed("booking_unresolved")
        try:
            cancelled = await self.backend.cancel(context, ref)
        except Exception as exc:  # noqa: BLE001
            return c.failed(f"calendar_error: {type(exc).__name__}")
        if cancelled is None:
            # Already gone on the provider side, or the provider refused the delete.
            return c.failed("cancel_rejected")
        return c.ok(cancelled)


class LookupRecordTool:
    tool_id = "lookup_record"
    display_name = "Look up a record"
    description = "Query the connected record source for an exact record."
    requires_connection: str | None = "record_source"
    requires_channels: frozenset[str] = frozenset()
    execution_mode = "in_process"
    required_inputs: frozenset[str] = frozenset({"query"})

    def __init__(self, backend: RecordBackend) -> None:
        self.backend = backend

    async def execute(self, inputs: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            matches = await self.backend.lookup(context, inputs["query"])
        except Exception as exc:  # noqa: BLE001
            return c.failed(f"record_source_error: {type(exc).__name__}")
        if not matches:
            return c.empty({"query": inputs["query"]})
        if len(matches) > 1:
            return c.ambiguous(matches)
        return c.ok({"record": matches[0]})


class SendEmailTool:
    tool_id = "send_email"
    display_name = "Send email"
    description = "Send an outbound email."
    requires_connection: str | None = "email_sender"
    requires_channels: frozenset[str] = frozenset({"email"})
    execution_mode = "in_process"
    required_inputs: frozenset[str] = frozenset({"to", "body"})

    def __init__(self, backend: MessengerBackend) -> None:
        self.backend = backend

    async def execute(self, inputs: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            msg_id = await self.backend.send(context, inputs["to"], inputs["body"])
        except Exception as exc:  # noqa: BLE001
            return c.failed(f"email_error: {type(exc).__name__}")
        if msg_id is None:
            return c.failed("provider_rejected")
        # ok = accepted-for-delivery (not inbox-delivered) — §3.3.6 honesty.
        return c.ok({"providerMessageId": msg_id, "state": "accepted_for_delivery"})


class SendSmsTool(SendEmailTool):
    tool_id = "send_sms"
    display_name = "Send SMS"
    description = "Send an outbound SMS."
    requires_connection: str | None = "sms_sender"
    requires_channels: frozenset[str] = frozenset({"sms"})


class ScheduleCallbackTool:
    tool_id = "schedule_callback"
    display_name = "Schedule a callback"
    description = "Queue a future outbound follow-up."
    # No connection at SCHEDULING time; delivery channel bound at fire time (§3.3.5).
    requires_connection: str | None = None
    requires_channels: frozenset[str] = frozenset()
    execution_mode = "in_process"
    required_inputs: frozenset[str] = frozenset({"fire_at", "channel", "message"})

    # Map from the free-form channel string to the connection_type checked at fire time
    # (§3.3.5: delivery channel bound at FIRE time, not scheduling time).
    # 2026-09-05 audit F144/F175: a callback is a MESSAGE delivered later over a
    # channel the lead left an address on. `widget` has no address to deliver to
    # (the visitor's tab is gone) and `voice` would be an outbound call the
    # platform does not make — both used to be accepted, queued, and then fail
    # every retry while Luciel had already promised the follow-up.
    _CHANNEL_TO_CONNECTION: dict[str, str] = {
        "email": "email_sender",
        "sms": "sms_sender",
        "whatsapp": "channel_auth",
    }

    async def _count_pending(
        self, session: Any, *, instance_id: Any, resolved_lead_id: Any = None
    ) -> int:
        """Count queued schedule_callback callbacks for this instance (optionally lead).

        RLS + the tenant session already scope rows to the caller's tenant; we filter
        by instance_id (and resolved_lead_id when a lead key is present) on top."""
        stmt = (
            select(func.count())
            .select_from(DeferredAction)
            .where(
                DeferredAction.instance_id == instance_id,
                DeferredAction.action_type == "schedule_callback",
                DeferredAction.status == "queued",
            )
        )
        if resolved_lead_id is not None:
            stmt = stmt.where(DeferredAction.resolved_lead_id == resolved_lead_id)
        return int((await session.execute(stmt)).scalar_one())

    async def _count_created_since(
        self, session: Any, *, instance_id: Any, since: datetime, resolved_lead_id: Any = None
    ) -> int:
        """Count schedule_callback rows created within the rolling window (any status)."""
        stmt = (
            select(func.count())
            .select_from(DeferredAction)
            .where(
                DeferredAction.instance_id == instance_id,
                DeferredAction.action_type == "schedule_callback",
                DeferredAction.created_at >= since,
            )
        )
        if resolved_lead_id is not None:
            stmt = stmt.where(DeferredAction.resolved_lead_id == resolved_lead_id)
        return int((await session.execute(stmt)).scalar_one())

    async def _opted_out_check(self, session: Any, context: ToolContext) -> ToolResult | None:
        """Refuse a callback for an opted-out lead, REGARDLESS of caps (§3.3.5).

        Lead.opted_out (STOP/HELP for SMS, §3.1.6) is read-only here — the column is
        owned/added by the core loop (migration 0017). Checked BEFORE the rate-limit
        axes and BEFORE the enqueue write, so an opted-out lead never gets a queued
        row no matter how far under cap the tenant/lead is. When resolved_lead_id is
        absent (anonymous session) there is no lead to check — falls through."""
        lead_id = context.resolved_lead_id
        if lead_id is None:
            return None
        lead = (
            await session.execute(
                select(Lead).where(
                    Lead.admin_id == context.admin_id,
                    Lead.id == lead_id,
                )
            )
        ).scalar_one_or_none()
        if lead is not None and lead.opted_out:
            return c.failed("opted_out")
        return None

    async def _rate_limit_check(self, session: Any, context: ToolContext) -> ToolResult | None:
        """Two-axis cap (§3.3.5): per-lead (when a lead key is present) + per-tenant,
        on pending count AND rolling-24h created count. Returns a structured failed
        outcome when over cap, else None. Counted BEFORE the enqueue write."""
        instance_id = context.instance_id
        lead_id = context.resolved_lead_id
        since = datetime.now(UTC) - _ROLLING_WINDOW

        # Per-lead axis — only when the lead identity is resolved (do NOT fabricate a
        # key; anonymous/unresolved sessions fall through to the per-tenant axis).
        if lead_id is not None:
            lead_pending = await self._count_pending(
                session, instance_id=instance_id, resolved_lead_id=lead_id
            )
            if lead_pending >= SCHEDULE_CALLBACK_PER_LEAD_PENDING_CAP:
                return c.failed("rate_limited:per_lead")
            lead_24h = await self._count_created_since(
                session, instance_id=instance_id, since=since, resolved_lead_id=lead_id
            )
            if lead_24h >= SCHEDULE_CALLBACK_PER_LEAD_24H_CAP:
                return c.failed("rate_limited:per_lead")

        # Per-tenant axis — enforced unconditionally (a ceiling per instance).
        tenant_pending = await self._count_pending(session, instance_id=instance_id)
        if tenant_pending >= SCHEDULE_CALLBACK_PER_TENANT_PENDING_CAP:
            return c.failed("rate_limited:per_tenant")
        tenant_24h = await self._count_created_since(session, instance_id=instance_id, since=since)
        if tenant_24h >= SCHEDULE_CALLBACK_PER_TENANT_24H_CAP:
            return c.failed("rate_limited:per_tenant")

        return None

    async def execute(self, inputs: dict[str, Any], context: ToolContext) -> ToolResult:
        """Durably enqueue a DeferredAction row (§3.3.5). ok ONLY after the write."""
        session = context.session
        if session is None:
            # Should never happen when dispatched via broker; guard against test misuse.
            return c.failed("enqueue_failed:no_session")

        channel: str = inputs["channel"]
        connection_type = self._CHANNEL_TO_CONNECTION.get(channel)
        if connection_type is None:
            # Refused before any write: the loop reads the structured reason and
            # offers a channel that can actually deliver (F144/F175).
            return c.failed(f"unsupported_channel:{channel}")

        # §3.3.5: an opted-out lead is NEVER sent a scheduled callback, regardless of
        # caps — checked first so opt-out always wins over an under-cap lead/tenant.
        opt_out_rejection = await self._opted_out_check(session, context)
        if opt_out_rejection is not None:
            return opt_out_rejection

        # §3.3.5 / Decision #51: enforce the two-axis rate limit BEFORE the enqueue
        # write, so an over-cap schedule NEVER persists a row and never nags the lead.
        # rate_limited is expressed as a STRUCTURED `failed` reason (§3.3.4) the loop
        # reasons about — NOT a new Outcome enum value (keeps the frozen contract). The
        # broker traces every outcome including this refusal (§3.3.4 rule 3).
        rejection = await self._rate_limit_check(session, context)
        if rejection is not None:
            return rejection

        try:
            await deferred_queue.enqueue(
                session,
                admin_id=context.admin_id,
                instance_id=context.instance_id,
                action_type="schedule_callback",
                connection_type=connection_type,
                resolved_lead_id=context.resolved_lead_id,
                payload={
                    "fire_at": inputs["fire_at"],
                    "channel": channel,
                    "message": inputs["message"],
                },
            )
        except Exception as exc:  # noqa: BLE001
            return c.failed(f"enqueue_failed:{type(exc).__name__}")

        return c.ok(
            {
                "scheduled": True,
                "fireAt": inputs["fire_at"],
                "channel": channel,
            }
        )


class PushToCrmTool:
    tool_id = "push_to_crm"
    display_name = "Push to CRM"
    description = "Push a captured lead to the connected CRM."
    requires_connection: str | None = "crm"
    requires_channels: frozenset[str] = frozenset()
    execution_mode = "in_process"
    required_inputs: frozenset[str] = frozenset({"lead_key", "fields"})

    def __init__(self, backend: CrmBackend) -> None:
        self.backend = backend

    async def execute(self, inputs: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            result = await self.backend.upsert_lead(context, inputs["lead_key"], inputs["fields"])
        except Exception as exc:  # noqa: BLE001
            # A CrmUnavailable carries a non-secret, stable reason (crm_key_not_email,
            # crm_lead_key_not_owned, hubspot_create_http_400 …) — the Leads page
            # shows it (F134); any other exception is named by class only.
            reason = (
                str(exc)
                if type(exc).__name__ == "CrmUnavailable" and str(exc)
                else type(exc).__name__
            )
            return c.failed(f"crm_error: {reason}")
        if result is None:
            # Field-mapping failure → failed; lead remains safe in our dashboard (§3.3.6).
            return c.failed("crm_field_mapping")
        # create-vs-update is part of the confirming payload (§3.3.6 traced
        # history): "updated" IS the maintenance semantics working.
        return c.ok(
            {
                "crmRecordId": result["crm_record_id"],
                "action": result.get("action", "updated"),
            }
        )


# bring_your_own_webhook is implemented in app/connections/sandbox.py (its execute
# routes through the subprocess sandbox), registered separately.

ALL_TOOL_IDS = (
    "check_availability",
    "book_appointment",
    "reschedule_appointment",
    "cancel_appointment",
    "send_email",
    "send_sms",
    "lookup_record",
    "schedule_callback",
    "push_to_crm",
    "bring_your_own_webhook",
)

# Static metadata used by the broker even before backends are bound.
# Owner-facing labels per tool id (round 6 WP-F: the Today card and the brief name
# capabilities the way Configure does). Derived from the classes above.
TOOL_DISPLAY_NAMES: dict[str, str] = {
    str(cls.tool_id): str(cls.display_name)
    for cls in list(globals().values())
    if isinstance(cls, type) and hasattr(cls, "tool_id") and hasattr(cls, "display_name")
}

TOOL_CONNECTION_TYPE: dict[str, str | None] = {
    "check_availability": "calendar",
    "book_appointment": "calendar",
    "reschedule_appointment": "calendar",
    "cancel_appointment": "calendar",
    "send_email": "email_sender",
    "send_sms": "sms_sender",
    "lookup_record": "record_source",
    "schedule_callback": None,
    "push_to_crm": "crm",
    "bring_your_own_webhook": "outbound_webhook",
}
TOOL_REQUIRED_INPUTS: dict[str, frozenset[str]] = {
    "check_availability": frozenset({"window"}),
    "book_appointment": frozenset({"slot", "email"}),
    "reschedule_appointment": frozenset({"email", "slot"}),
    "cancel_appointment": frozenset({"email"}),
    "send_email": frozenset({"to", "body"}),
    "send_sms": frozenset({"to", "body"}),
    "lookup_record": frozenset({"query"}),
    "schedule_callback": frozenset({"fire_at", "channel", "message"}),
    "push_to_crm": frozenset({"lead_key", "fields"}),
    "bring_your_own_webhook": frozenset({"payload"}),
}

# --- capability grouping (Decision #8) ---------------------------------------- #
# SCHEDULING IS ONE CAPABILITY, not four unrelated toggles. All four scheduling tools
# read and write the SINGLE ``calendar`` connection (§3.8.2 allows one active connection
# per type), so they connect once and are gated together: the connection that lets Luciel
# offer a time is the same one that lets it move or cancel that time. Grouping them here
# keeps that truth in one place — the UI renders the group, and the disconnect cascade
# (Decision #5b) disables the whole group when the calendar goes away.
SCHEDULING_TOOL_IDS: tuple[str, ...] = (
    "check_availability",
    "book_appointment",
    "reschedule_appointment",
    "cancel_appointment",
)


@dataclass(frozen=True)
class CapabilitySpec:
    """One owner-facing capability and the tool ids that implement it.

    ``connection_type`` is DERIVED from TOOL_CONNECTION_TYPE rather than restated, so a
    group can never drift from the connection its tools actually use.
    """

    capability: str
    label: str
    help_text: str
    tool_ids: tuple[str, ...]

    @property
    def connection_type(self) -> str | None:
        return TOOL_CONNECTION_TYPE[self.tool_ids[0]]


CAPABILITIES: tuple[CapabilitySpec, ...] = (
    CapabilitySpec(
        capability="scheduling",
        label="Appointment scheduling",
        help_text=(
            "Luciel can offer open times, book an appointment, and move or cancel one "
            "it already booked — all on the calendar you connect here."
        ),
        tool_ids=SCHEDULING_TOOL_IDS,
    ),
)

CAPABILITY_GROUPS: dict[str, tuple[str, ...]] = {
    spec.capability: spec.tool_ids for spec in CAPABILITIES
}


def capability_of(tool_id: str) -> str | None:
    """The capability group a tool belongs to, or None if it stands alone."""
    for capability, tool_ids in CAPABILITY_GROUPS.items():
        if tool_id in tool_ids:
            return capability
    return None


__all__ = [
    "CheckAvailabilityTool",
    "BookAppointmentTool",
    "RescheduleAppointmentTool",
    "CancelAppointmentTool",
    "LookupRecordTool",
    "SendEmailTool",
    "SendSmsTool",
    "ScheduleCallbackTool",
    "PushToCrmTool",
    "ALL_TOOL_IDS",
    "TOOL_CONNECTION_TYPE",
    "TOOL_REQUIRED_INPUTS",
    "SCHEDULING_TOOL_IDS",
    "CapabilitySpec",
    "CAPABILITIES",
    "CAPABILITY_GROUPS",
    "capability_of",
    "Outcome",
    "Tool",
]
