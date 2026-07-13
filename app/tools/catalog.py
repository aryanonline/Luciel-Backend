"""Built-in tool catalog (Architecture §3.3.2) — the 8 admin-selectable tools.

Each tool declares its required connection_type, channels, execution mode, and
required inputs, and implements the five-outcome result contract (§3.3.4/§3.3.6).
External effects (calendar, CRM, record source, send) are reached through an
injected backend so the per-tool accuracy behavior is testable without real
providers; the real backends bind via the Connections layer (Arc 17).
"""

from __future__ import annotations

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


class RecordBackend(Protocol):
    async def lookup(self, ctx: ToolContext, query: str) -> list[dict[str, Any]]: ...


class MessengerBackend(Protocol):
    async def send(self, ctx: ToolContext, to: str, body: str) -> str | None: ...


class CrmBackend(Protocol):
    async def upsert_lead(
        self, ctx: ToolContext, lead_key: str, fields: dict[str, Any]
    ) -> str | None: ...


# --- tools ------------------------------------------------------------------ #
class CheckAvailabilityTool:
    tool_id = "check_availability"
    display_name = "Check availability"
    description = "Read the connected calendar's free/busy to find open slots."
    requires_connection = "calendar"
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
    requires_connection = "calendar"
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


class LookupRecordTool:
    tool_id = "lookup_record"
    display_name = "Look up a record"
    description = "Query the connected record source for an exact record."
    requires_connection = "record_source"
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
    requires_connection = "email_sender"
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
    requires_connection = "sms_sender"
    requires_channels: frozenset[str] = frozenset({"sms"})


class ScheduleCallbackTool:
    tool_id = "schedule_callback"
    display_name = "Schedule a callback"
    description = "Queue a future outbound follow-up."
    # No connection at SCHEDULING time; delivery channel bound at fire time (§3.3.5).
    requires_connection = None
    requires_channels: frozenset[str] = frozenset()
    execution_mode = "in_process"
    required_inputs: frozenset[str] = frozenset({"fire_at", "channel", "message"})

    # Map from the free-form channel string to the connection_type checked at fire time
    # (§3.3.5: delivery channel bound at FIRE time, not scheduling time).
    _CHANNEL_TO_CONNECTION: dict[str, str] = {
        "email": "email_sender",
        "sms": "sms_sender",
        "whatsapp": "channel_auth",
        "voice": "channel_auth",
        "widget": "channel_auth",
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
        connection_type = self._CHANNEL_TO_CONNECTION.get(channel, "channel_auth")

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
    requires_connection = "crm"
    requires_channels: frozenset[str] = frozenset()
    execution_mode = "in_process"
    required_inputs: frozenset[str] = frozenset({"lead_key", "fields"})

    def __init__(self, backend: CrmBackend) -> None:
        self.backend = backend

    async def execute(self, inputs: dict[str, Any], context: ToolContext) -> ToolResult:
        try:
            crm_id = await self.backend.upsert_lead(context, inputs["lead_key"], inputs["fields"])
        except Exception as exc:  # noqa: BLE001
            return c.failed(f"crm_error: {type(exc).__name__}")
        if crm_id is None:
            # Field-mapping failure → failed; lead remains safe in our dashboard (§3.3.6).
            return c.failed("crm_field_mapping")
        return c.ok({"crmRecordId": crm_id})


# bring_your_own_webhook is implemented in app/connections/sandbox.py (its execute
# routes through the subprocess sandbox), registered separately.

ALL_TOOL_IDS = (
    "check_availability",
    "book_appointment",
    "send_email",
    "send_sms",
    "lookup_record",
    "schedule_callback",
    "push_to_crm",
    "bring_your_own_webhook",
)

# Static metadata used by the broker even before backends are bound.
TOOL_CONNECTION_TYPE: dict[str, str | None] = {
    "check_availability": "calendar",
    "book_appointment": "calendar",
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
    "send_email": frozenset({"to", "body"}),
    "send_sms": frozenset({"to", "body"}),
    "lookup_record": frozenset({"query"}),
    "schedule_callback": frozenset({"fire_at", "channel", "message"}),
    "push_to_crm": frozenset({"lead_key", "fields"}),
    "bring_your_own_webhook": frozenset({"payload"}),
}

__all__ = [
    "CheckAvailabilityTool",
    "BookAppointmentTool",
    "LookupRecordTool",
    "SendEmailTool",
    "SendSmsTool",
    "ScheduleCallbackTool",
    "PushToCrmTool",
    "ALL_TOOL_IDS",
    "TOOL_CONNECTION_TYPE",
    "TOOL_REQUIRED_INPUTS",
    "Outcome",
    "Tool",
]
