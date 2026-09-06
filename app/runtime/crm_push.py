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
  * The native CRMs (HubSpot, Salesforce) key their dedupe on the email
    property/field, so only EMAIL-keyed leads push to them; pushing a phone
    number into an email key would create garbage records the tenant then
    cleans up. A CUSTOM WEBHOOK is the tenant's own endpoint and dedupes on
    whatever stable key we send, so a phone-only lead reaches it keyed on its
    E.164 number (2026-09-05 audit F126). Leads with neither stay safe in the
    dashboard store.
  * A failed push changes nothing about the lead's facts (they are already
    persisted in the dashboard) — the outcome lands in the durable tool trace
    (C6) AND on the lead row (``crm_push_status``, F134) so the Leads page can
    say "not in your CRM" and offer a retry; the owner is emailed once per
    lead. The reply to the customer is never blocked or altered by it.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lead import Lead
from app.runtime.lead_capture import CaptureResult, crm_phone_key

logger = logging.getLogger(__name__)

PUSH_TOOL_ID = "push_to_crm"
# Failure reasons that mean the CRM itself refused or could not be reached. A
# broker REFUSAL (tool off, connection not connected/dormant) is not a failed
# push — nothing was attempted — and never marks the lead or mails the owner.
CRM_FAILURE_PREFIXES = ("crm_error", "crm_field_mapping", "tool_exception")


@dataclass(frozen=True)
class PushReceipt:
    """What one attempted push came back with (the broker's outcome + reason)."""

    outcome: str
    reason: str | None = None

    @property
    def crm_failed(self) -> bool:
        """The CRM itself refused or could not be reached (vs. a broker refusal)."""
        return self.outcome == "failed" and (self.reason or "").startswith(CRM_FAILURE_PREFIXES)

    @property
    def refusal_reason(self) -> str | None:
        """A plain-words reason when the push was REFUSED before reaching the CRM."""
        if self.outcome == "ok" or self.crm_failed:
            return None
        reason = self.reason or ""
        if reason == "not_authorized":
            return "the Push to CRM tool is switched off"
        if reason.startswith("connection_not_ready:"):
            status = reason.rsplit(":", 1)[-1].replace("_", " ")
            return f"your CRM connection is not ready ({status})"
        return reason or "the push was refused"


async def _crm_provider(
    session: AsyncSession, admin_id: uuid.UUID, instance_id: uuid.UUID
) -> str | None:
    from app.models.connection import InstanceConnection  # noqa: PLC0415

    provider = (
        await session.execute(
            select(InstanceConnection.provider).where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.instance_id == instance_id,
                InstanceConnection.connection_type == "crm",
            )
        )
    ).scalar_one_or_none()
    return str(provider) if provider else None


async def _stamp_and_notify(
    session: AsyncSession, *, admin_id: uuid.UUID, lead: Lead, outcome: str, reason: str | None
) -> None:
    """Record the honest CRM state on the lead (F134); email the owner once per lead."""
    now = datetime.now(UTC)
    if outcome == "ok":
        lead.crm_push_status = "synced"
        lead.crm_push_detail = None
        lead.crm_pushed_at = now
        return
    if outcome != "failed" or not (reason or "").startswith(CRM_FAILURE_PREFIXES):
        return  # a refusal: nothing was attempted, nothing to report as broken
    lead.crm_push_status = "failed"
    lead.crm_push_detail = (reason or "failed")[:160]
    if lead.crm_failure_notified_at is not None:
        return
    from app.models.instance import Account  # noqa: PLC0415
    from app.notifications import email_dispatcher  # noqa: PLC0415

    owner_email = (
        await session.execute(select(Account.email).where(Account.id == admin_id))
    ).scalar_one_or_none()
    if not owner_email:
        return
    try:
        await email_dispatcher.send_crm_push_failed(
            session,
            admin_id=admin_id,
            to=owner_email,
            lead_label=lead.name or "a new lead",
            detail=lead.crm_push_detail or "failed",
        )
    except Exception:  # noqa: BLE001 — the notice must never break the turn
        logger.exception("crm_push_failure_notice_failed")
        return
    lead.crm_failure_notified_at = now


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
) -> PushReceipt | None:
    """Push this turn's changed lead facts to the tenant's CRM, broker-gated.

    Returns the dispatch receipt (outcome + reason) or None when no push was
    attempted. Quiet no-op whenever the push cannot or should not happen: nothing
    changed, the lead has no key the connected CRM accepts, the tool is outside
    this conversation's snapshot, or no backend resolves. Broker refusals
    (disabled tool, disconnected or dormant connection) resolve inside dispatch
    and land in the tool trace — exactly as a model-proposed call would."""
    changed = dict(captured.changed)
    # `contact` and `email` are the CRM KEY, not CRM fields — the key rides as
    # lead_key, never in the payload. (When a phone-keyed lead's volunteered
    # email arrives, capture enriches `changed` with the record's known facts,
    # so stripping the key here never empties the first pushable moment.)
    changed.pop("contact", None)
    changed.pop("email", None)
    if not changed:
        return None
    if snapshot_tool_ids is not None and PUSH_TOOL_ID not in snapshot_tool_ids:
        return None
    tool = tool_resolver(PUSH_TOOL_ID) if tool_resolver else None
    if tool is None:
        return None
    lead = (
        await session.execute(
            select(Lead).where(Lead.admin_id == admin_id, Lead.id == captured.lead_id)
        )
    ).scalar_one_or_none()
    key = lead_key if lead_key and "@" in lead_key else None
    if key is None and lead is not None:
        # F126: the tenant's own webhook dedupes on whatever stable key we send.
        if await _crm_provider(session, admin_id, instance_id) == "custom_webhook":
            key = crm_phone_key(lead)
    if key is None:
        return None

    from app.tools.broker import ToolBroker  # noqa: PLC0415

    broker = ToolBroker(session)
    result = await broker.dispatch(
        tool,
        {"lead_key": key, "fields": changed},
        admin_id=admin_id,
        instance_id=instance_id,
        resolved_lead_id=captured.lead_id,
        session_id=session_id,
    )
    outcome = result.outcome.value
    if outcome != "ok":
        # The lead is safe in the dashboard store regardless; the trace row is
        # the admin-facing record of the refused/failed push.
        logger.info("crm_push_on_capture_not_ok outcome=%s", outcome)
    if lead is not None:
        await _stamp_and_notify(
            session, admin_id=admin_id, lead=lead, outcome=outcome, reason=result.reason
        )
    return PushReceipt(outcome=outcome, reason=result.reason)
