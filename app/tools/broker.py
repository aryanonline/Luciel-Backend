"""Tool broker — two-gate dispatch + result contract (Architecture §3.3.3, §3.3.4).

Dispatch gate order:
  Gate 1 (Authorization, default-deny): a live instance_tool_authorizations row for
    (instance, tool) with enabled=true. Absent → structured refusal.
  Gate 2 (Connection): if the tool declares requires_connection, a live
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
from app.models.tool_auth import InstanceToolAuthorization
from app.tools import contract as c
from app.tools.contract import Tool, ToolContext, ToolResult

ACTION_TOOLS = frozenset(
    {"book_appointment", "send_email", "send_sms", "schedule_callback", "push_to_crm"}
)


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
    ) -> ToolResult:
        # Gate 1 — Authorization (default-deny).
        if not await self._authorized(admin_id, instance_id, tool.tool_id):
            result = c.failed("not_authorized")
            await self._trace(admin_id, instance_id, tool.tool_id, result, connection=None)
            return result

        # Gate 2 — Connection (only if the tool requires one).
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
        ctx = ToolContext(admin_id=admin_id, instance_id=instance_id, connection_status=conn_status)
        try:
            result = await tool.execute(inputs, ctx)
        except Exception as exc:  # noqa: BLE001
            result = c.failed(f"tool_exception:{type(exc).__name__}")

        await self._trace(
            admin_id, instance_id, tool.tool_id, result, connection=tool.requires_connection
        )
        return result

    # Collected outcome traces (the trace store §5.4 persists these in Arc 14).
    # Kept in-memory on the broker for the loop + tests; NOT written to the audit
    # log with a fabricated event_type — the §5.2 enum has no tool-dispatch event,
    # and §3.3.4 rule 3 "every outcome is traced" is the trace store's job (§5.4),
    # not the business audit log. Flagged: persist to trace_store in Arc 14.
    traces: list[dict[str, Any]]

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
