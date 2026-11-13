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
from app.employee.context import build_operating_context
from app.models.instance import Instance, channel_enabled, is_paused
from app.runtime.knowledge_retrieval import (
    make_chunk_attributor,
    make_retriever,
    make_retriever_bundle,
)
from app.runtime.orchestrator import AT_CAP_REPLY, Orchestrator
from app.runtime.provider_factory import build_llm_router
from app.runtime.sessions import (
    effective_config,
    find_open_session,
    participant_key_for,
    resolve_or_open_session,
)
from app.tools.registry import make_tool_resolver

# Strong-identity channels: the transport identifier IS a stable lead key, so it is
# passed to the loop for lead capture + identity resolution (§3.4.4/§3.4.9). The widget
# is weak-identity and passes None; these channels carry a real sender address/id.
_STRONG_TRANSPORT_CHANNELS = {
    "sms",
    "voice",
    "email",
    "whatsapp",
    "messenger",
    "instagram",
    # Legacy combined id (pre-§3.1.2 split) — old in-flight sessions keep their class.
    "instagram_messenger",
}


@dataclass
class RuntimeReply:
    reply_text: str
    render_state: str  # active | paused | at_cap
    session_id: str
    #: The turn opened this session (first message of a new conversation, §3.4.8) —
    #: what the SMS edge keys the one-time AI-identity + STOP notice on (F064).
    is_new_session: bool = False
    #: A person holds the conversation (§3.4.12): no auto-reply went out, and none
    #: must be fabricated by the caller (F057).
    handed_off: bool = False


def names(inst: Instance) -> tuple[str, str]:
    """Luciel display name + assistant speaker name (§3.4.16). PUBLIC: the voice
    pipeline reuses this and ``disclosure_business_name`` (2026-08-18) rather than
    minting a third copy beside chat_widget's mirror.

    Mirrors app.api.v1.chat_widget._names. NOTE ON NAMING: the first element
    returned here (historically called "business") is ``inst.name`` -- the
    LUCIEL's own configured name, NOT the business's name. It must never be
    fed into the AI-identity disclosure's business noun; see
    ``_disclosure_business_name`` for that (the bug this fixes: the disclosure
    naming the Luciel, e.g. "Maya's Front-Desk Assistant", instead of the
    business).

    When no assistantName is configured, the Luciel name itself is used
    VERBATIM as the speaker — never suffixed with
    "'s assistant", which is what produced "...Assistant's assistant" for a
    business/Luciel name already ending in the word "Assistant".
    """
    business = inst.name or "this business"
    personality = inst.personality or {}
    assistant = str(personality.get("assistantName") or business)
    return business, assistant


def disclosure_business_name(inst: Instance) -> str:
    """Resolve the BUSINESS name for the AI-identity disclosure clause (§3.4.16).

    Mirrors app.api.v1.chat_widget._disclosure_business_name. Checked the
    Instance model and the Personality pillar for a usable SHORT business-name
    field: there isn't one -- ``personality.businessContext``
    (app.runtime.personality.business_context_of) and the signup
    ``Instance.business_one_liner`` are both free-text up to 280 chars, meant
    for the system prompt, not a short display name. With no usable short
    name, this falls back to the generic "this business". The Luciel name
    (``inst.name``) must NEVER be returned here.
    """
    return (inst.business_short_name or "").strip() or "this business"


def drain_refused() -> RuntimeReply:
    """The honest drain reply for a NEW conversation on a non-serving Luciel or a
    switched-off channel (§3.6.2 / §3.6.3 step 0): no session, no LLM, nothing said.

    Empty text is deliberate — the channel webhooks 200-no-op it (the doc's "channel
    webhooks return HTTP 200 with a no-op body"), so the provider does not retry and
    nothing is fabricated on the tenant's number or address. ``render_state`` says
    WHY the reply is empty, so an edge that needs to (the widget) can render the quiet
    paused directive instead of an error, and a test can tell this refusal from an
    LLM that happened to say nothing."""
    return RuntimeReply(reply_text="", render_state="paused", session_id="")


async def _in_flight(session: AsyncSession, msg: InboundMessage) -> bool:
    """Does this inbound belong to a conversation that was already open when the
    gate closed? Read-only — the drain admits, it never opens."""
    draining = await find_open_session(
        session,
        admin_id=msg.admin_id,
        instance_id=msg.instance_id,
        channel=msg.channel,
        session_id=msg.session_id,
        participant_key=participant_key_for(msg.admin_id, msg.channel, msg.customer_identifier),
    )
    return draining is not None


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

    # Paused / deleting → no NEW session, no LLM (§3.6.2 / §3.6.3). Same predicate the
    # widget and voice paths use: an owner who pauses or deletes Luciel must be paused
    # on every channel, not just the web. The carve-out (C8): a conversation already
    # in flight at the pause/delete instant finishes on its channel — the drain gate
    # admits ONLY an existing live in-window session, never opens one. Reaching this
    # gate at all on a non-widget channel needs the inbound resolver to still route
    # the tenant's number / address / Meta id here — which is why a delete writes
    # ``quiescing`` (routable, draining) on every connection rather than ``revoked``
    # (unroutable): before that, the drain on every channel but the widget was
    # silence forever for a customer who was mid-text (round 7 WP-5, item 1).
    if is_paused(inst) and not await _in_flight(session, msg):
        return drain_refused()

    # A channel the owner switched OFF admits no NEW conversation (audit F058); a
    # conversation already in flight when it was switched off finishes on its channel
    # (§3.8.7 rule E.1 — the safety half of a config change never cuts a lead off
    # mid-sentence). Same drain gate as pause: an existing live session only.
    if not channel_enabled(inst, msg.channel) and not await _in_flight(session, msg):
        return drain_refused()

    # Conversation = ONE billed session (§3.4.8). The transport identifier the adapter
    # already resolved (From number / from_address / Meta-scoped id) is the session key,
    # so a thread's later messages land in the SAME session inside the channel's
    # inactivity window instead of opening — and billing — a new conversation per
    # message (the owner-reported 10-texts-=-10-conversations defect).
    sess, is_new = await resolve_or_open_session(
        session,
        admin_id=msg.admin_id,
        instance_id=msg.instance_id,
        channel=msg.channel,
        resolved_lead_id=None,
        session_id=msg.session_id,
        participant_key=participant_key_for(msg.admin_id, msg.channel, msg.customer_identifier),
    )

    retriever = make_retriever(session, admin_id=msg.admin_id, instance_id=msg.instance_id)
    # Harmony wave 2, item 2a: real chunk attribution alongside the retriever, for
    # the same reason as the widget path (app/api/v1/chat_widget.py).
    chunk_attributor = make_chunk_attributor(
        session, admin_id=msg.admin_id, instance_id=msg.instance_id
    )
    effective = effective_config(sess.config_snapshot, inst, always_include_channel=msg.channel)
    business, assistant = names(inst)

    orch = Orchestrator(
        session=session,
        router=build_llm_router(admin_id=msg.admin_id),
        retriever=retriever,
        chunk_attributor=chunk_attributor,
        retriever_bundle=make_retriever_bundle(
            session, admin_id=msg.admin_id, instance_id=msg.instance_id
        ),
        admin_id=msg.admin_id,
        instance_id=msg.instance_id,
        enabled_channels=effective.enabled_channels,
        # The source the inbound edge rate-limits this sender under (F086).
        abuse_source=(msg.channel, msg.customer_identifier or ""),
        # `business` above is inst.name (the Luciel's own name) -- never pass it as
        # business_name for the disclosure backstop; use _disclosure_business_name.
        business_name=disclosure_business_name(inst),
        assistant_name=assistant,
        business_context=effective.business_context,
        personality=effective.personality,
        # Real card state, not a literal False: a PAYG tenant must keep serving past the
        # free 50 on every channel (§12.9 / Vision §7).
        has_payment_method=await metering.has_live_card(session, admin_id=msg.admin_id),
        snapshot_tool_ids=effective.snapshot_tool_ids,
        tool_resolver=make_tool_resolver(),
        # The employee's operating context (round 6 WP-F).
        operating_context=await build_operating_context(session, inst, admin_id=msg.admin_id),
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
        # Every adapter stamps received_at at ingestion; the lead transcript row
        # carries it so reply-time analytics measure real latency (2026-08-17).
        received_at=msg.received_at,
    )

    # The graceful at-cap line is the AT-CAP reply, nothing else's: a human-controlled
    # session returns an empty reply on purpose (§3.4.12 — the person answers, not
    # Luciel), and coalescing it into the at-cap text fabricated a bot line on every
    # channel during every takeover (audit F057/F145).
    return RuntimeReply(
        reply_text=result.reply_text or (AT_CAP_REPLY if result.at_cap else ""),
        render_state="at_cap" if result.at_cap else "active",
        session_id=str(sess.id),
        is_new_session=is_new,
        handed_off=result.handed_off,
    )
