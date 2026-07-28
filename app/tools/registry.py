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

Connection-gated (dispatch path is correct; the effect needs an OAuth backend that
does not exist yet, so they resolve to a Tool and fail cleanly rather than silently):
  * ``check_availability`` / ``book_appointment`` — calendar (Calendly / Google).
  * ``lookup_record``                            — admin-connected record source.
  * ``push_to_crm``                              — HubSpot / Salesforce.
  * ``bring_your_own_webhook``                   — admin-registered HTTP endpoint.

For the connection-gated set the broker's gate 3 refuses first (the connection can
never reach ``connected`` without a real OAuth flow), so the unimplemented backend is
only reachable if a connection is marked connected out of band. In that case it raises
:class:`BackendNotConnected` and the catalog's own ``except`` maps it to a structured
``failed`` — never a fabricated success (§3.3.4 rule 1).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from app.channels.twilio_sender import build_sms_sender
from app.connections import sandbox
from app.notifications.email_sender import build_email_sender
from app.tools import catalog
from app.tools import contract as c
from app.tools.contract import Tool, ToolContext, ToolResult

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

    async def lookup(self, ctx: ToolContext, query: str) -> list[dict[str, Any]]:
        raise BackendNotConnected("record_source_backend_unavailable")

    async def upsert_lead(
        self, ctx: ToolContext, lead_key: str, fields: dict[str, Any]
    ) -> str | None:
        raise BackendNotConnected("crm_backend_unavailable")


class _EmailMessengerBackend:
    """MessengerBackend over the platform email sender (SES live / Null in mock).

    Vision §3.2: the ``send_email`` *tool* is Luciel proactively reaching a lead on a
    channel they are not currently on — so the subject is platform-composed, not
    model-authored.
    """

    SUBJECT = "A message from your assistant"

    async def send(self, ctx: ToolContext, to: str, body: str) -> str | None:
        sender = build_email_sender()
        return await sender.send(to=to, subject=self.SUBJECT, body_text=body) or None


class _SmsMessengerBackend:
    """MessengerBackend over the platform SMS sender (Twilio live / Null in mock).

    ``from_number`` is resolved by the SMS sender itself in live mode; the tool has no
    per-lead sender choice to make, and gate 2 has already proved the SMS channel is
    enabled before this runs (the send_sms↔SMS-channel coupling, §3.3.3 gate 2).
    """

    async def send(self, ctx: ToolContext, to: str, body: str) -> str | None:
        sender = build_sms_sender()
        return await sender.send_sms("", to, body)


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
    requires_connection = "outbound_webhook"
    requires_channels: frozenset[str] = frozenset()
    execution_mode = "subprocess"
    required_inputs: frozenset[str] = frozenset({"payload"})

    def __init__(self) -> None:
        # One breaker per tool instance == per turn. A cross-turn breaker needs a
        # durable failure store; until then the sandbox's per-call timeout and the
        # allowlist are the load-bearing protections.
        self._breaker = sandbox.CircuitBreaker()

    async def execute(self, inputs: dict[str, Any], context: ToolContext) -> ToolResult:
        config = await self._connection_config(context)
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
            auth_header=None,  # secrets stay in the SecretStore; §3.8.4 binds them later
            allowlist=allowlist,
            breaker=self._breaker,
            executor=sandbox.SubprocessWebhookExecutor(),
            now=time.monotonic(),
            context=context,
        )

    async def _connection_config(self, context: ToolContext) -> dict[str, object]:
        from sqlalchemy import select  # noqa: PLC0415 — keeps import cost off module load

        from app.models.connection import InstanceConnection  # noqa: PLC0415

        row = (
            await context.session.execute(
                select(InstanceConnection.non_secret_config).where(
                    InstanceConnection.admin_id == context.admin_id,
                    InstanceConnection.instance_id == context.instance_id,
                    InstanceConnection.connection_type == "outbound_webhook",
                )
            )
        ).scalar_one_or_none()
        return dict(row) if isinstance(row, dict) else {}


def build_tool(tool_id: str) -> Tool | None:
    """Construct the production Tool for ``tool_id``, or None if it is not a tool.

    A fresh instance per resolution: the tools are stateless apart from the BYO
    breaker, and per-turn construction keeps no cross-tenant state.
    """
    unavailable = _UnavailableExternalBackend()
    if tool_id == "schedule_callback":
        return catalog.ScheduleCallbackTool()
    if tool_id == "send_email":
        return catalog.SendEmailTool(_EmailMessengerBackend())
    if tool_id == "send_sms":
        return catalog.SendSmsTool(_SmsMessengerBackend())
    if tool_id == "check_availability":
        return catalog.CheckAvailabilityTool(unavailable)
    if tool_id == "book_appointment":
        return catalog.BookAppointmentTool(unavailable)
    if tool_id == "lookup_record":
        return catalog.LookupRecordTool(unavailable)
    if tool_id == "push_to_crm":
        return catalog.PushToCrmTool(unavailable)
    if tool_id == "bring_your_own_webhook":
        return BringYourOwnWebhookTool()
    return None


def make_tool_resolver() -> Callable[[str], Tool | None]:
    """The ``ToolResolver`` the orchestrator calls to turn a proposed tool_id into a
    Tool. Resolution is deliberately authorization-blind — the broker owns all three
    gates (§3.3.3), so resolving is not permitting."""
    return build_tool
