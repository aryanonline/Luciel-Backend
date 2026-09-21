"""Production tool resolver — binds the catalog's tools to real backends (§3.3.2).

The catalog (``app/tools/catalog.py``) declares each tool's prerequisites and result
mapping but takes its external effect through an injected backend. Until now nothing
in ``app/`` constructed those bindings, so the orchestrator's ``tool_resolver`` was
None in production and *every* tool call short-circuited to ``failed``. This module is
the missing binding layer: one resolver, used by both production entrypoints.

Functional today (a real effect, no external OAuth needed):
  * ``schedule_callback`` — writes a DeferredAction row through the deferred queue.
  * ``send_email``        — Amazon SES via ``build_email_sender`` (Null in mock/CI).
  * ``send_sms``          — Twilio via ``build_sms_sender`` (Null in mock/CI).

Connection-backed (a real effect once the tenant has completed the OAuth connect):
  * the four scheduling tools — Google Calendar via ``_TenantCalendarBackend``, which
    picks the backend from the tenant's connected calendar provider at call time.

Connection-backed (a real effect once the tenant has connected):
  * ``bring_your_own_webhook`` — admin-registered HTTP endpoint through the
    subprocess sandbox, with the admin's stored Authorization header attached
    by the parent process at dispatch (the subprocess never sees other secrets).
  * ``push_to_crm``   — HubSpot contact / Salesforce Lead upsert (maintains the
    SAME lead's record via ``crm_lead_links``, §3.3.6) or the custom-webhook
    sandbox path; all in ``app/tools/crm_backend.py``.
  * ``lookup_record`` — the admin's uploaded CSV rows, read live at call time
    (``app/tools/record_backend.py``).

Connection-gated (dispatch path is correct; the effect needs an OAuth backend that
does not exist yet, so they resolve to a Tool and fail cleanly rather than silently):
  * the scheduling tools on a CALENDLY connection — no booking backend yet
    (honest-disabled in the registry, audit round 3 Phase 4).
  * ``lookup_record`` on a LIVE-API connector (non-CSV) — no effect layer yet.

For the connection-gated set the broker's gate 3 refuses first (the connection can
never reach ``connected`` without a real OAuth flow), so the unimplemented backend is
only reachable if a connection is marked connected out of band. In that case it raises
:class:`BackendNotConnected` and the catalog's own ``except`` maps it to a structured
``failed`` — never a fabricated success (§3.3.4 rule 1).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from app.channels.twilio_sender import build_sms_sender
from app.connections import sandbox
from app.notifications.email_sender import build_email_sender
from app.runtime.integration_status import null_in_live
from app.tools import calendar_backend, catalog
from app.tools import contract as c
from app.tools.contract import Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from app.tools.calendly_backend import CalendlyBackend

# Tools whose real effect is reachable today with no external OAuth backend.
FUNCTIONAL_TOOL_IDS = frozenset({"schedule_callback", "send_email", "send_sms"})
# Tools whose dispatch path is wired but whose effect needs an OAuth backend (§3.8.4).
CONNECTION_GATED_TOOL_IDS = frozenset(
    {
        "check_availability",
        "book_appointment",
        "lookup_record",
        "push_to_crm",
        "bring_your_own_webhook",
    }
)


_CALENDAR_TOOLS: dict[str, Callable[[Any], Tool]] = {
    "check_availability": catalog.CheckAvailabilityTool,
    "book_appointment": catalog.BookAppointmentTool,
    "reschedule_appointment": catalog.RescheduleAppointmentTool,
    "cancel_appointment": catalog.CancelAppointmentTool,
}


class BackendNotConnected(RuntimeError):
    """Raised when a tool's external backend has no production implementation.

    The catalog wraps every backend call, so this surfaces as a structured ``failed``
    with a non-secret reason. It exists so an unimplemented backend can never be
    mistaken for an ``empty`` result (which would read to the lead as "no slots" /
    "no such record" — a false statement about the business's data).
    """


class _UnavailableExternalBackend:
    """Stands in for calendar / record-source / CRM backends until Connections can
    complete a real OAuth flow. Every method raises so the outcome is ``failed``."""

    async def free_busy(self, ctx: ToolContext, window: str) -> list[str]:
        raise BackendNotConnected("calendar_backend_unavailable")

    async def book(self, ctx: ToolContext, slot: str, email: str) -> dict[str, Any] | None:
        raise BackendNotConnected("calendar_backend_unavailable")

    async def find_bookings(
        self, ctx: ToolContext, *, email: str, booking_ref: str | None = None
    ) -> list[dict[str, Any]]:
        raise BackendNotConnected("calendar_backend_unavailable")

    async def reschedule(
        self, ctx: ToolContext, booking_ref: str, slot: str
    ) -> dict[str, Any] | None:
        raise BackendNotConnected("calendar_backend_unavailable")

    async def cancel(self, ctx: ToolContext, booking_ref: str) -> dict[str, Any] | None:
        raise BackendNotConnected("calendar_backend_unavailable")

    async def lookup(self, ctx: ToolContext, query: str) -> list[dict[str, Any]]:
        raise BackendNotConnected("record_source_backend_unavailable")

    async def upsert_lead(
        self, ctx: ToolContext, lead_key: str, fields: dict[str, Any]
    ) -> dict[str, Any] | None:
        raise BackendNotConnected("crm_backend_unavailable")


class _TenantCalendarBackend:
    """Dispatches the calendar tools on the PROVIDER the tenant actually connected.

    ``build_tool`` takes only a tool_id, so which calendar a tool talks to cannot be
    decided at construction — it is a per-tenant fact read from the connection row at
    call time. Google Calendar gets the real backend; Calendly has no registered OAuth
    client yet, so it keeps raising and stays honestly unavailable rather than
    pretending to book into a calendar we cannot reach.
    """

    def __init__(self) -> None:
        self._unavailable = _UnavailableExternalBackend()
        self._google: calendar_backend.GoogleCalendarBackend | None = None
        self._calendly: CalendlyBackend | None = None

    async def _for(
        self, ctx: ToolContext
    ) -> _UnavailableExternalBackend | calendar_backend.GoogleCalendarBackend | CalendlyBackend:
        provider = await self._calendar_provider(ctx)
        if provider == "google_calendar":
            if self._google is None:
                self._google = calendar_backend.GoogleCalendarBackend()
            return self._google
        if provider == "calendly":
            # Round 6 WP-H (F055): Calendly books through a scheduling link the customer
            # completes; the backend says so in every payload rather than claiming a write.
            if self._calendly is None:
                from app.tools.calendly_backend import CalendlyBackend  # noqa: PLC0415

                self._calendly = CalendlyBackend()
            return self._calendly
        return self._unavailable

    @staticmethod
    async def _calendar_provider(ctx: ToolContext) -> str | None:
        if ctx.session is None:
            return None
        from sqlalchemy import select  # noqa: PLC0415 — keeps import cost off module load

        from app.models.connection import InstanceConnection  # noqa: PLC0415

        provider = (
            await ctx.session.execute(
                select(InstanceConnection.provider).where(
                    InstanceConnection.admin_id == ctx.admin_id,
                    InstanceConnection.instance_id == ctx.instance_id,
                    InstanceConnection.connection_type == "calendar",
                )
            )
        ).scalar_one_or_none()
        return str(provider) if provider is not None else None

    async def free_busy(self, ctx: ToolContext, window: str) -> list[str]:
        return await (await self._for(ctx)).free_busy(ctx, window)

    async def book(self, ctx: ToolContext, slot: str, email: str) -> dict[str, Any] | None:
        return await (await self._for(ctx)).book(ctx, slot, email)

    async def find_bookings(
        self, ctx: ToolContext, *, email: str, booking_ref: str | None = None
    ) -> list[dict[str, Any]]:
        return await (await self._for(ctx)).find_bookings(ctx, email=email, booking_ref=booking_ref)

    async def reschedule(
        self, ctx: ToolContext, booking_ref: str, slot: str
    ) -> dict[str, Any] | None:
        return await (await self._for(ctx)).reschedule(ctx, booking_ref, slot)

    async def cancel(self, ctx: ToolContext, booking_ref: str) -> dict[str, Any] | None:
        return await (await self._for(ctx)).cancel(ctx, booking_ref)


class _TenantCrmBackend:
    """Dispatches ``push_to_crm`` on the PROVIDER the tenant actually connected
    (same pattern as ``_TenantCalendarBackend``): HubSpot and Salesforce get
    the real native upsert backends (same link/heal discipline, Phase 6.5),
    and the custom webhook posts through the sandbox envelope."""

    def __init__(self) -> None:
        self._unavailable = _UnavailableExternalBackend()

    async def upsert_lead(
        self, ctx: ToolContext, lead_key: str, fields: dict[str, Any]
    ) -> dict[str, Any] | None:
        provider = await _connection_provider(ctx, "crm")
        if provider == "hubspot":
            from app.tools.crm_backend import HubSpotCrmBackend  # noqa: PLC0415

            return await HubSpotCrmBackend().upsert_lead(ctx, lead_key, fields)
        if provider == "salesforce":
            from app.tools.crm_backend import SalesforceCrmBackend  # noqa: PLC0415

            return await SalesforceCrmBackend().upsert_lead(ctx, lead_key, fields)
        if provider == "custom_webhook":
            from app.tools.crm_backend import WebhookCrmBackend  # noqa: PLC0415

            return await WebhookCrmBackend().upsert_lead(ctx, lead_key, fields)
        return await self._unavailable.upsert_lead(ctx, lead_key, fields)


class _TenantRecordBackend:
    """Dispatches ``lookup_record`` on the connected record-source provider:
    ``csv`` reads the uploaded rows live at call time; ``live_connector`` reads the
    owner's own endpoint through the SSRF seam (round 6 WP-H, F062)."""

    def __init__(self) -> None:
        self._unavailable = _UnavailableExternalBackend()

    async def lookup(self, ctx: ToolContext, query: str) -> list[dict[str, Any]]:
        from app.connections import registry as provider_registry  # noqa: PLC0415

        provider = await _connection_provider(ctx, "record_source")
        if provider == "csv":
            from app.tools.record_backend import CsvRecordBackend  # noqa: PLC0415

            return await CsvRecordBackend().lookup(ctx, query)
        # Round 7 WP-4a item 1: dispatch on the id the CONNECT FLOW writes
        # (``live_connector``, the registered provider). WP-H keyed this on "api",
        # which the registry never offered, so every live connector read "connected"
        # and every lookup fell through to unavailable. ``canonical_provider`` keeps a
        # row written under the old id resolving.
        if provider is not None and provider_registry.canonical_provider(provider) == (
            "live_connector"
        ):
            from app.tools.record_backend import LiveRecordBackend  # noqa: PLC0415

            return await LiveRecordBackend().lookup(ctx, query)
        return await self._unavailable.lookup(ctx, query)


async def _connection_provider(ctx: ToolContext, connection_type: str) -> str | None:
    if ctx.session is None:
        return None
    from sqlalchemy import select  # noqa: PLC0415 — keeps import cost off module load

    from app.models.connection import InstanceConnection  # noqa: PLC0415

    provider = (
        await ctx.session.execute(
            select(InstanceConnection.provider).where(
                InstanceConnection.admin_id == ctx.admin_id,
                InstanceConnection.instance_id == ctx.instance_id,
                InstanceConnection.connection_type == connection_type,
            )
        )
    ).scalar_one_or_none()
    return str(provider) if provider is not None else None


class _EmailMessengerBackend:
    """MessengerBackend over the platform email sender (SES live / Null in mock).

    Vision §3.2: the ``send_email`` *tool* is Luciel proactively reaching a lead on a
    channel they are not currently on — so the subject is platform-composed, not
    model-authored.
    """

    SUBJECT = "A message from your assistant"

    async def send(self, ctx: ToolContext, to: str, body: str) -> str | None:
        # Round 6 WP-H (F141): with a session, the email goes out the same way a reply
        # does — through the tenant's OWN mailbox (Outlook) or their legacy provisioned
        # address, with the AI-identity footer, never from a platform address behind the
        # owner's back. No mailbox connected → provider_rejected, honestly.
        if ctx.session is not None:
            from app.channels.outbound import send_outbound_email  # noqa: PLC0415

            receipt = await send_outbound_email(
                ctx.session,
                ctx.admin_id,
                ctx.instance_id,
                to=to,
                body=body,
                subject=self.SUBJECT,
            )
            if not receipt.accepted:
                return None
            # Graph's 202 carries no id; an accepted send still needs a truthful marker.
            return receipt.provider_message_id or "mailbox:accepted"
        sender = build_email_sender()
        # A Null sender in live mode cannot put anything on the wire. Returning an id
        # would make the tool answer ok/accepted_for_delivery and Luciel would tell the
        # lead out loud that the email was sent (§3.3.4 faithful readback).
        if null_in_live(sender):
            return None
        return await sender.send(to=to, subject=self.SUBJECT, body_text=body) or None


class _SmsMessengerBackend:
    """MessengerBackend over the TENANT's own Twilio account (§3.1.4 BYO).

    The tool has no per-lead sender choice to make, and gate 2 has already proved the SMS
    channel is enabled before this runs (the send_sms↔SMS-channel coupling, §3.3.3 gate
    2). Which ACCOUNT it sends through is not a platform choice either: it is the Twilio
    the admin connected, sending from the number they designated. Without a session the
    tenant's credential cannot be read, so the platform fallback stays (Null in mock).
    """

    async def send(self, ctx: ToolContext, to: str, body: str) -> str | None:
        if ctx.session is None:
            platform_sender = build_sms_sender()
            if null_in_live(platform_sender):
                return None
            return await platform_sender.send_sms("", to, body)
        from app.channels.tenant_twilio import build_tenant_sms_sender
        from app.connections.service import resolve_sms_number

        from_number = await resolve_sms_number(ctx.session, ctx.admin_id, ctx.instance_id)
        if not from_number:
            return None
        sender = await build_tenant_sms_sender(ctx.session, ctx.admin_id, ctx.instance_id)
        # See _EmailMessengerBackend: an unusable tenant credential must surface as
        # provider_rejected, never as a fabricated accepted-for-delivery.
        if null_in_live(sender):
            return None
        return await sender.send_sms(from_number, to, body)


class BringYourOwnWebhookTool:
    """The BYO-webhook tool (§3.3.8) — the Tool class the catalog said lived
    "elsewhere" and did not. Executes through the subprocess sandbox envelope
    (SSRF allowlist + circuit breaker + hard 10s kill) in ``connections/sandbox.py``.

    The endpoint URL, auth header and egress allowlist come from the connection's
    ``non_secret_config``; without a connected ``outbound_webhook`` connection the
    broker's gate 3 refuses before this runs.
    """

    tool_id = "bring_your_own_webhook"
    display_name = "Custom webhook"
    description = "Call the admin-registered HTTP endpoint registered as a tool."
    requires_connection: str | None = "outbound_webhook"
    requires_channels: frozenset[str] = frozenset()
    execution_mode = "subprocess"
    required_inputs: frozenset[str] = frozenset({"payload"})

    def __init__(self) -> None:
        # One breaker per tool instance == per turn. A cross-turn breaker needs a
        # durable failure store; until then the sandbox's per-call timeout and the
        # allowlist are the load-bearing protections.
        self._breaker = sandbox.CircuitBreaker()

    async def execute(self, inputs: dict[str, Any], context: ToolContext) -> ToolResult:
        config, secret_ref = await self._connection_config(context)
        url = str(config.get("url") or "")
        if not url:
            return c.failed("webhook_url_not_configured")
        raw_allowlist = config.get("egressAllowlist")
        hosts = raw_allowlist if isinstance(raw_allowlist, list) else []
        allowlist = frozenset(str(h) for h in hosts if str(h))
        payload = inputs["payload"]
        return await sandbox.run_byo_webhook(
            url=url,
            payload=dict(payload) if isinstance(payload, dict) else {"value": payload},
            # The admin-registered Authorization header, read from the
            # SecretStore HERE (the Data Plane process) and attached by the
            # sandbox before the subprocess submits — the subprocess never sees
            # raw token bytes (§3.8.6). Hardcoding None meant every
            # authenticated endpoint 401'd while the tool looked configured.
            auth_header=await self._auth_header(secret_ref),
            allowlist=allowlist,
            breaker=self._breaker,
            executor=sandbox.SubprocessWebhookExecutor(),
            now=time.monotonic(),
            context=context,
        )

    async def _connection_config(
        self, context: ToolContext
    ) -> tuple[dict[str, object], str | None]:
        from sqlalchemy import select  # noqa: PLC0415 — keeps import cost off module load

        from app.models.connection import InstanceConnection  # noqa: PLC0415

        row = (
            await context.session.execute(
                select(InstanceConnection.non_secret_config, InstanceConnection.secret_ref).where(
                    InstanceConnection.admin_id == context.admin_id,
                    InstanceConnection.instance_id == context.instance_id,
                    InstanceConnection.connection_type == "outbound_webhook",
                )
            )
        ).one_or_none()
        if row is None:
            return {}, None
        config, secret_ref = row
        return (dict(config) if isinstance(config, dict) else {}), secret_ref

    @staticmethod
    async def _auth_header(secret_ref: str | None) -> str | None:
        if not secret_ref:
            return None
        from app.connections.secrets import get_secret_store  # noqa: PLC0415

        material = await get_secret_store().get(secret_ref) or {}
        header = material.get("auth_header", "")
        return header or None


def build_tool(tool_id: str) -> Tool | None:
    """Construct the production Tool for ``tool_id``, or None if it is not a tool.

    A fresh instance per resolution: the tools are stateless apart from the BYO
    breaker, and per-turn construction keeps no cross-tenant state.
    """
    if tool_id == "schedule_callback":
        return catalog.ScheduleCallbackTool()
    if tool_id == "send_email":
        return catalog.SendEmailTool(_EmailMessengerBackend())
    if tool_id == "send_sms":
        return catalog.SendSmsTool(_SmsMessengerBackend())
    if tool_id in _CALENDAR_TOOLS:
        return _CALENDAR_TOOLS[tool_id](_TenantCalendarBackend())
    if tool_id == "lookup_record":
        return catalog.LookupRecordTool(_TenantRecordBackend())
    if tool_id == "push_to_crm":
        return catalog.PushToCrmTool(_TenantCrmBackend())
    if tool_id == "bring_your_own_webhook":
        return BringYourOwnWebhookTool()
    return None


def make_tool_resolver() -> Callable[[str], Tool | None]:
    """The ``ToolResolver`` the orchestrator calls to turn a proposed tool_id into a
    Tool. Resolution is deliberately authorization-blind — the broker owns all three
    gates (§3.3.3), so resolving is not permitting."""
    return build_tool
