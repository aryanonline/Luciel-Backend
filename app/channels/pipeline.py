"""Channel inbound edge pipeline (Architecture §3.1.3 + §3.1.5).

Order, per the doctrine: verify signature → resolve tenant → rate-limit/abuse
admission (PRE-BUDGET) → normalize to InboundMessage. Rejected traffic never
reaches the budget gate or an LLM call, and abuse/auto-block events are audited
(widget_abuse_blocked / channel_abuse_blocked).

The runtime hand-off (budget gate + agentic loop) is Arc 14; this edge produces
the normalized InboundMessage and the admission decision.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.channels import rate_limit
from app.channels.base import ChannelAdapter, InboundMessage, InboundRejected, InstanceContext
from app.db.session import tenant_session
from app.models.audit import ActorType

logger = logging.getLogger("luciel.channels.inbound")


@dataclass
class AdmissionDenied:
    reason: str
    channel: str
    source_id: str


@dataclass
class OptOutHandled:
    """An inbound STOP/HELP/START keyword the platform handles at the channel edge.

    The customer's keyword is suppressed from the orchestrator/LLM path (§3.1.6,
    Legal §A2); the carrier (Twilio Messaging Services) sends the canonical opt-out /
    help reply. We surface the classified keyword so the caller can record consent
    state without ever invoking the runtime (audit finding Axis6-F05).
    """

    keyword: str  # stop|help|start
    channel: str
    customer_identifier: str
    # Enough for the edge to send the platform's own confirmation on channels with no
    # carrier keyword handling (F074) — tenant, instance, and the identity to reply as.
    admin_id: uuid.UUID | None = None
    instance_id: uuid.UUID | None = None
    sender_id: str | None = None
    subject: str | None = None


@dataclass(frozen=True)
class ContactReply:
    """An inbound from one of the tenant's OWN escalation contacts (audit F051).

    An owner or manager replying to the escalation text on the tenant's number is not
    a lead: no session opens, nothing is billed, the runtime never runs. Surfaced so
    the edge can record the acknowledgement.
    """

    admin_id: uuid.UUID
    instance_id: uuid.UUID
    channel: str
    contact_role: str
    body: str


def _normalize_number(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit() or ch == "+")


async def _escalation_contact_role(
    ctx: InstanceContext, channel: str, customer_identifier: str
) -> str | None:
    """'primary' / 'secondary' when the sender is a configured SMS escalation contact."""
    # Texts only: a contact CALLING the tenant's line is a caller like any other (the
    # voice route answers or declines on its own gates); a TEXT from the contact's
    # number is the reply to the escalation page.
    if channel != "sms" or not customer_identifier:
        return None
    from app.models.instance import Instance  # noqa: PLC0415

    async with tenant_session(ctx.admin_id) as s:
        cfg = (
            await s.execute(select(Instance.escalation).where(Instance.id == ctx.instance_id))
        ).scalar_one_or_none()
    if not isinstance(cfg, dict):
        return None
    sender = _normalize_number(customer_identifier)
    for role, key in (("primary", "primarySms"), ("secondary", "secondarySms")):
        number = cfg.get(key)
        if isinstance(number, str) and number and _normalize_number(number) == sender:
            return role
    return None


async def ingest(
    adapter: ChannelAdapter,
    request: dict[str, Any],
    session: AsyncSession,
) -> InboundMessage | AdmissionDenied | OptOutHandled | ContactReply | InboundRejected:
    """Run the inbound edge. Returns:
    - InboundMessage on admission (ready for the runtime budget gate, Arc 14),
    - OptOutHandled when the inbound is a STOP/HELP/START keyword (suppressed pre-budget),
    - AdmissionDenied when rate-limited / abuse-blocked (audited, pre-budget),
    - raises/returns InboundRejected on signature/resolution failure (audited).
    """
    # 1. Verify signature + resolve tenant (raises InboundRejected on failure).
    ctx = await adapter.verify_inbound(request)

    # 2. Normalize enough to know the source identifier for rate-limiting.
    msg = await adapter.receive(request, ctx)
    source_id = msg.customer_identifier or request.get("source_ip", "unknown")

    # 2a. The tenant's own escalation contact writing back on the tenant's number
    # (audit F051): the owner's side of an escalation, never a lead.
    contact_role = await _escalation_contact_role(ctx, adapter.channel_id, msg.customer_identifier)
    if contact_role is not None:
        return ContactReply(
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            channel=adapter.channel_id,
            contact_role=contact_role,
            body=msg.body,
        )

    # 2b. Opt-out / help keyword handling (§3.1.6) BEFORE the orchestrator/LLM. The
    # carrier sends the canonical reply; we never route the keyword to the runtime.
    opt_keyword = msg.channel_metadata.get("opt_keyword")
    if opt_keyword:
        # Round 5C item 8: RECORD the consent state, not just suppress the
        # keyword — Lead.opted_out gates scheduled callbacks (§3.3.5) and was
        # never written by any runtime path. Only an existing lead is touched:
        # a bare STOP from a number we never captured has no record to update
        # (and schedule_callback requires a captured lead anyway).
        kw = str(opt_keyword)
        if kw in ("stop", "start") and msg.customer_identifier:
            from app.models.lead import Lead  # noqa: PLC0415

            async with tenant_session(ctx.admin_id) as s:
                lead = (
                    await s.execute(
                        select(Lead).where(
                            Lead.admin_id == ctx.admin_id,
                            Lead.instance_id == ctx.instance_id,
                            Lead.contact_identifier == msg.customer_identifier,
                        )
                    )
                ).scalar_one_or_none()
                if lead is not None:
                    opted_out = kw == "stop"
                    if lead.opted_out != opted_out:
                        lead.opted_out = opted_out
                        await audit.emit(
                            s,
                            admin_id=ctx.admin_id,
                            instance_id=ctx.instance_id,
                            actor_type=ActorType.platform,
                            event_type="lead_opt_out_recorded",
                            payload={
                                "channel": adapter.channel_id,
                                "keyword": kw,
                                "opted_out": opted_out,
                            },
                        )
        return OptOutHandled(
            keyword=str(opt_keyword),
            channel=adapter.channel_id,
            customer_identifier=msg.customer_identifier,
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            sender_id=(
                msg.channel_metadata.get("destination_id")
                or msg.channel_metadata.get("phone_number_id")
            ),
            subject=msg.channel_metadata.get("subject"),
        )

    # 3. Pre-budget admission (per-source token bucket + per-tenant ceiling).
    decision = rate_limit.admit(adapter.channel_id, source_id, str(ctx.admin_id))
    if not decision.allowed:
        event = (
            "widget_abuse_blocked" if adapter.channel_id == "widget" else "channel_abuse_blocked"
        )
        # Audit the block under the resolved tenant (no PII: hashed source not stored raw).
        async with tenant_session(ctx.admin_id) as s:
            await audit.emit(
                s,
                admin_id=ctx.admin_id,
                instance_id=ctx.instance_id,
                actor_type=ActorType.platform,
                event_type=event,
                payload={"channel": adapter.channel_id, "reason": decision.reason},
            )
        return AdmissionDenied(
            reason=decision.reason or "rate_limited",
            channel=adapter.channel_id,
            source_id=source_id,
        )

    return msg


async def record_inbound_rejection(
    session: AsyncSession, *, reason: str, channel: str, source_hint: str | None = None
) -> None:
    """An unsigned/unresolvable inbound is rejected and audit-logged (§3.1.3).

    These occur BEFORE tenant resolution, so there is no tenant scope; we therefore
    do NOT write a tenant audit row (writing under a guessed tenant would itself be
    a cross-tenant action). Instead we emit a structured WARNING to platform
    observability (the prod log pipeline ships these to CloudWatch). No PII is
    logged — only the channel, the rejection reason, and an OPTIONAL caller-supplied
    hash hint (never the raw source identifier). Audit finding Axis6-F06.
    """
    # Interpolated into the message, not passed via ``extra=``: nothing configures a
    # formatter that renders extras, so those fields never reached CloudWatch.
    logger.warning(
        "inbound_rejected channel=%s reason=%s source_hint=%s", channel, reason, source_hint
    )
    return None
