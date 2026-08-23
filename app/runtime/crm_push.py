"""Deterministic CRM push-on-capture (audit round 3, Phase 6.5).

The Customer Journey promises the CRM "keeps each lead's record current" as new
facts surface. Model-initiated ``push_to_crm`` calls (the live tool loop) can do
that — but ONLY when the model remembers, and a promise kept by model memory is
not kept. This step makes it deterministic cognition: when a turn's lead capture
actually changed a fact, the change is pushed through the SAME broker dispatch a
model call would use, so every gate holds unchanged — default-deny authorization
(tool disabled → nothing happens), the conversation's frozen capability
snapshot (§3.8.7 rule E), and connection health including dormant.

Scope rules, stated honestly:
  * Only EMAIL-keyed leads push. The native CRM transports key their dedupe on
    the email property/field (HubSpot ``email``, Salesforce ``Email``); pushing
    a phone number into an email key would create garbage records the tenant
    then cleans up. Phone-keyed leads stay safe in the dashboard store.
  * A failed push changes nothing about the lead (it is already persisted in
    the dashboard) — the outcome lands in the durable tool trace (C6) and the
    connection's own health surfaces the broken credential; the reply to the
    customer is never blocked or altered by it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.runtime.lead_capture import CaptureResult

logger = logging.getLogger(__name__)

PUSH_TOOL_ID = "push_to_crm"


async def push_captured_lead(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    captured: CaptureResult,
    lead_key: str | None,
    snapshot_tool_ids: frozenset[str] | set[str] | None,
    tool_resolver: Callable[[str], Any] | None,
    session_id: uuid.UUID | None,
) -> None:
    """Push this turn's changed lead facts to the tenant's CRM, broker-gated.

    Quiet no-op whenever the push cannot or should not happen: nothing changed,
    the lead has no email key, the tool is outside this conversation's snapshot,
    or no backend resolves. Broker refusals (disabled tool, disconnected or
    dormant connection) resolve inside dispatch and land in the tool trace —
    exactly as a model-proposed call would."""
    changed = dict(captured.changed)
    # The stable CRM key is the email; `contact` inside changed is that same key
    # on first capture and is not a separate CRM field.
    changed.pop("contact", None)
    if not changed:
        return
    if not lead_key or "@" not in lead_key:
        return
    if snapshot_tool_ids is not None and PUSH_TOOL_ID not in snapshot_tool_ids:
        return
    tool = tool_resolver(PUSH_TOOL_ID) if tool_resolver else None
    if tool is None:
        return

    from app.tools.broker import ToolBroker  # noqa: PLC0415

    broker = ToolBroker(session)
    result = await broker.dispatch(
        tool,
        {"lead_key": lead_key, "fields": changed},
        admin_id=admin_id,
        instance_id=instance_id,
        resolved_lead_id=captured.lead_id,
        session_id=session_id,
    )
    if result.outcome.value != "ok":
        # The lead is safe in the dashboard store regardless; the trace row is
        # the admin-facing record of the refused/failed push.
        logger.info("crm_push_on_capture_not_ok outcome=%s", result.outcome.value)
