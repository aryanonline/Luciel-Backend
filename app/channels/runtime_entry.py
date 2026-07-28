"""Shared channel → runtime hand-off (Architecture §3.4.1 / §3.1).

The inbound edge (`pipeline.ingest`) verifies + resolves + rate-limits and produces a
normalized ``InboundMessage``. This module takes that message the rest of the way:
open/resume the session, build the orchestrator with the tenant's effective config, run
one turn, and return the reply text + render state. It is the non-widget-channel analogue
of the widget's in-handler runtime block, factored so SMS/email/WhatsApp/Instagram-
Messenger all share ONE runtime path rather than four drifting copies.

(The widget keeps its own inline copy for now; this helper is additive and does not touch
it — a future refactor can point the widget here too.)

Paused instance → empty reply, no LLM (§3.6.2). At-cap is handled inside the loop
(graceful no-LLM reply, §3.4.1b) and surfaced via render_state.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing import metering
from app.channels.base import InboundMessage
from app.models.instance import Instance
from app.runtime.knowledge_retrieval import make_retriever
from app.runtime.orchestrator import AT_CAP_REPLY, Orchestrator
from app.runtime.provider_factory import build_llm_router
from app.runtime.sessions import effective_config, resolve_or_open_session
from app.tools.registry import make_tool_resolver

# Strong-identity channels: the transport identifier IS a stable lead key, so it is
# passed to the loop for lead capture + identity resolution (§3.4.4/§3.4.9). The widget
# is weak-identity and passes None; these channels carry a real sender address/id.
_STRONG_TRANSPORT_CHANNELS = {"sms", "voice", "email", "whatsapp", "instagram_messenger"}


@dataclass
class RuntimeReply:
    reply_text: str
    render_state: str  # active | paused | at_cap
    session_id: str


def _names(inst: Instance) -> tuple[str, str]:
    business = inst.name or "this business"
    personality = inst.personality or {}
    assistant = str(personality.get("assistantName") or f"{business}'s assistant")
    return business, assistant


async def run_inbound_message(session: AsyncSession, msg: InboundMessage) -> RuntimeReply:
    """Run an admitted InboundMessage through the agentic loop; return the reply.

    Assumes the caller already ran ``pipeline.ingest`` (signature verified, tenant
    resolved, rate-limit admitted) and is inside a tenant_session bound to msg.admin_id.
    """
    inst = (
        await session.execute(select(Instance).where(Instance.id == msg.instance_id))
    ).scalar_one_or_none()
    if inst is None:
        # Should not happen post-resolution, but never route into a missing instance.
        return RuntimeReply(reply_text="", render_state="paused", session_id="")

    # Paused → no session, no LLM (§3.6.2).
    if getattr(inst, "status", None) == "paused" or getattr(inst, "paused_at", None):
        return RuntimeReply(reply_text="", render_state="paused", session_id="")

    sess, _is_new = await resolve_or_open_session(
        session,
        admin_id=msg.admin_id,
        instance_id=msg.instance_id,
        channel=msg.channel,
        resolved_lead_id=None,
    )

    retriever = make_retriever(session, admin_id=msg.admin_id, instance_id=msg.instance_id)
    effective = effective_config(sess.config_snapshot, inst, always_include_channel=msg.channel)
    business, assistant = _names(inst)

    orch = Orchestrator(
        session=session,
        router=build_llm_router(admin_id=msg.admin_id),
        retriever=retriever,
        admin_id=msg.admin_id,
        instance_id=msg.instance_id,
        enabled_channels=effective.enabled_channels,
        business_name=business,
        assistant_name=assistant,
        business_context=effective.business_context,
        personality=effective.personality,
        # Real card state, not a literal False: a PAYG tenant must keep serving past the
        # free 50 on every channel (§12.9 / Vision §7).
        has_payment_method=await metering.has_live_card(session, admin_id=msg.admin_id),
        snapshot_tool_ids=effective.snapshot_tool_ids,
        tool_resolver=make_tool_resolver(),
    )

    transport_identifier = (
        msg.customer_identifier if msg.channel in _STRONG_TRANSPORT_CHANNELS else None
    )
    result = await orch.run_turn(
        session_id=sess.id,
        resolved_lead_id=None,
        channel=msg.channel,
        mode=sess.mode,
        inbound_text=msg.body,
        transport_identifier=transport_identifier,
    )

    return RuntimeReply(
        reply_text=result.reply_text or AT_CAP_REPLY,
        render_state="at_cap" if result.at_cap else "active",
        session_id=str(sess.id),
    )
