"""Tool broker — three-gate dispatch + result contract (Architecture §3.3.3, §3.3.4).

Dispatch gate order:
  Gate 1 (Authorization, default-deny): a live instance_tool_authorizations row for
    (instance, tool) with enabled=true. Absent → structured refusal.
  Gate 2 (Channel prerequisite, §3.3.3): if the tool declares requires_channels,
    every named channel must be enabled-and-connected for this instance. Absent or
    disabled channel → structured tool-error channel_not_enabled:{channel_id}, same
    shape as gate 3 (§3.3.3 gate 2 / Decision §43).
  Gate 3 (Connection): if the tool declares requires_connection, a live
    instance_connections row for that type with status='connected'. Otherwise a
    structured tool-error (NOT a 500, NOT a silent failure) so the agentic loop
    reasons about it uniformly (§3.8.7 rule A / §3.4.1).

Pre-execution: required-input validation → missing_input (§3.3.7 broker backstop),
NOT a failure and NOT an escalation trigger.

Every outcome is traced/audited (§3.3.4 rule 3). The broker never lets a tool
silently fail or raise an unstructured 500.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connections import gate
from app.models.instance import Instance
from app.models.tool_auth import InstanceToolAuthorization
from app.tools import contract as c
from app.tools.contract import Tool, ToolContext, ToolResult

# The write-tool set lives with the schemas (single source, audit round 3 C3);
# re-exported here for callers that reach it via the broker.
from app.tools.schemas import ACTION_TOOLS as ACTION_TOOLS  # noqa: PLC0414


class ToolBroker:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def _authorized(self, admin_id: uuid.UUID, instance_id: uuid.UUID, tool_id: str) -> bool:
        row = (
            await self.s.execute(
                select(InstanceToolAuthorization).where(
                    InstanceToolAuthorization.admin_id == admin_id,
                    InstanceToolAuthorization.instance_id == instance_id,
                    InstanceToolAuthorization.tool_id == tool_id,
                    InstanceToolAuthorization.enabled.is_(True),
                )
            )
        ).scalar_one_or_none()
        return row is not None

    async def _channel_enabled(
        self, admin_id: uuid.UUID, instance_id: uuid.UUID, channel_id: str
    ) -> bool:
        """Gate 2 — channel prerequisite (§3.3.3, Decision §43).

        A channel is enabled-and-connected for the instance when the instance's
        ``channels`` JSONB contains an entry for the channel_id with
        ``enabled: true``.  An absent entry OR ``enabled: false`` is a hard gate.
        This is a LIVE read (§3.8.7 rule E safety side) — the same session the
        broker runs in, no caching."""
        row = (
            await self.s.execute(
                select(Instance.channels).where(
                    Instance.admin_id == admin_id,
                    Instance.id == instance_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return False
        channels: list[dict[str, object]] = row or []
        for ch in channels:
            if ch.get("id") == channel_id and ch.get("enabled") is True:
                return True
        return False

    async def _connection_status(
        self, admin_id: uuid.UUID, instance_id: uuid.UUID, connection_type: str
    ) -> str | None:
        # Delegates to the universal gate (§3.8.7 rule A) — one source of truth for
        # what "connected" means. Reads LIVE status so a mid-conversation revoke is
        # immediate (§3.8.7 rule E safety side). Behavior is identical to the prior
        # inline check (revoked rows excluded, returns None when unconfigured).
        return await gate.connection_status(
            self.s, admin_id=admin_id, instance_id=instance_id, connection_type=connection_type
        )

    async def dispatch(
        self,
        tool: Tool,
        inputs: dict[str, Any],
        *,
        admin_id: uuid.UUID,
        instance_id: uuid.UUID,
        resolved_lead_id: uuid.UUID | None = None,
        session_id: uuid.UUID | None = None,
    ) -> ToolResult:
        # Dispatch-scoped conversation handle for the durable trace rows (C6). The
        # broker is per-request and the loop awaits each dispatch, so plain instance
        # state is race-free here and keeps five _trace call sites unchanged.
        self._session_id = session_id
        # Gate 1 — Authorization (default-deny).
        if not await self._authorized(admin_id, instance_id, tool.tool_id):
            result = c.failed("not_authorized")
            await self._trace(admin_id, instance_id, tool.tool_id, result, connection=None)
            return result

        # Gate 2 — Channel prerequisite (§3.3.3, Decision §43).
        # Only applies when the tool declares requires_channels (non-empty frozenset).
        if tool.requires_channels:
            for channel_id in sorted(tool.requires_channels):  # deterministic order
                if not await self._channel_enabled(admin_id, instance_id, channel_id):
                    result = c.failed(f"channel_not_enabled:{channel_id}")
                    await self._trace(
                        admin_id,
                        instance_id,
                        tool.tool_id,
                        result,
                        connection=tool.requires_connection,
                    )
                    return result

        # Gate 3 — Connection (only if the tool requires one).
        conn_status: str | None = None
        if tool.requires_connection is not None:
            conn_status = await self._connection_status(
                admin_id, instance_id, tool.requires_connection
            )
            if conn_status != gate.CONNECTED:
                # Structured tool-error of the same shape as a refusal (§3.3.3), built
                # by the shared gate so the reason string is one source of truth
                # (§3.8.7 rule A): connection_not_ready:{type}:{status}.
                result = c.failed(gate.gate_failure_reason(tool.requires_connection, conn_status))
                await self._trace(
                    admin_id, instance_id, tool.tool_id, result, connection=tool.requires_connection
                )
                return result

        # Pre-execution required-input validation → missing_input (§3.3.7).
        missing = [f for f in tool.required_inputs if f not in inputs or inputs[f] in (None, "")]
        if missing:
            result = c.missing_input(sorted(missing))
            await self._trace(
                admin_id, instance_id, tool.tool_id, result, connection=tool.requires_connection
            )
            return result

        # Execute. Any unexpected raise is normalized to a structured `failed`,
        # never a 500/silent failure (§3.8.7 rule D synchronous path).
        ctx = ToolContext(
            admin_id=admin_id,
            instance_id=instance_id,
            connection_status=conn_status,
            session=self.s,  # §3.3.5: tools that need a durable write receive the session
            resolved_lead_id=resolved_lead_id,  # §3.3.5: per-lead cap key for schedule_callback
        )
        try:
            result = await tool.execute(inputs, ctx)
        except Exception as exc:  # noqa: BLE001
            result = c.failed(f"tool_exception:{type(exc).__name__}")

        # Durable booking fact for the analytics card (§3.9): written at the ONE
        # point every dispatch passes through, and only on `ok` — a metric fed
        # by the same event that told the customer "booked" can never disagree
        # with what Luciel said (§3.3.4 rule 1: no success-claim without ok, and
        # its converse: no metric without a success-claim).
        if tool.tool_id == "book_appointment" and result.outcome is c.Outcome.ok:
            from app.models.booking import BookingEvent  # noqa: PLC0415

            self.s.add(
                BookingEvent(
                    admin_id=admin_id,
                    instance_id=instance_id,
                    booking_ref=str(result.payload.get("bookingRef") or ""),
                )
            )

        await self._trace(
            admin_id, instance_id, tool.tool_id, result, connection=tool.requires_connection
        )
        return result

    # In-memory mirror of the traces this broker instance wrote (kept for the loop +
    # existing tests). The DURABLE record is the tool_trace_events row written below
    # (§5.4 / §3.3.4 rule 3, C6) — the previous in-memory-only list died with the
    # request, so "every outcome is traced" was never actually persisted.
    traces: list[dict[str, Any]]
    _session_id: uuid.UUID | None

    async def _trace(
        self,
        admin_id: uuid.UUID,
        instance_id: uuid.UUID,
        tool_id: str,
        result: ToolResult,
        *,
        connection: str | None,
    ) -> None:
        if not hasattr(self, "traces"):
            self.traces = []
        self.traces.append(
            {
                "tool": tool_id,
                "outcome": result.outcome.value,
                "connection": connection,
                "missing_fields": result.missing_fields or None,
                "reason": result.reason,  # non-secret
            }
        )
        # Durable trace row (C6). Field NAMES and non-secret reasons only — never
        # customer-supplied input values (§3.4.11 PII posture).
        from app.models.trace import ToolTraceEvent  # noqa: PLC0415

        self.s.add(
            ToolTraceEvent(
                admin_id=admin_id,
                instance_id=instance_id,
                session_id=getattr(self, "_session_id", None),
                tool_id=tool_id,
                outcome=result.outcome.value,
                connection_type=connection,
                missing_fields=list(result.missing_fields) or None,
                reason=result.reason or None,
            )
        )
