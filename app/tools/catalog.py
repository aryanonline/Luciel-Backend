"""Built-in tool catalog (Architecture §3.3.2) — the 8 admin-selectable tools.

Each tool declares its required connection_type, channels, execution mode, and
required inputs, and implements the five-outcome result contract (§3.3.4/§3.3.6).
External effects (calendar, CRM, record source, send) are reached through an
injected backend so the per-tool accuracy behavior is testable without real
providers; the real backends bind via the Connections layer (Arc 17).
"""

from __future__ import annotations

from typing import Any, Protocol

from app.connections import deferred_queue
from app.tools import contract as c
from app.tools.contract import Outcome, Tool, ToolContext, ToolResult


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

    async def execute(self, inputs: dict[str, Any], context: ToolContext) -> ToolResult:
        """Durably enqueue a DeferredAction row (§3.3.5). ok ONLY after the write."""
        session = context.session
        if session is None:
            # Should never happen when dispatched via broker; guard against test misuse.
            return c.failed("enqueue_failed:no_session")

        channel: str = inputs["channel"]
        connection_type = self._CHANNEL_TO_CONNECTION.get(channel, "channel_auth")

        try:
            await deferred_queue.enqueue(
                session,
                admin_id=context.admin_id,
                instance_id=context.instance_id,
                action_type="schedule_callback",
                connection_type=connection_type,
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
