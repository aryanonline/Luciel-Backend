"""Shared inbound dispatch: pipeline → runtime → out-of-band reply (§3.4.1).

Factored out of the webhook module so the two ways a message ARRIVES share one way
it is HANDLED:

  * provider webhooks (Twilio / SES-SNS / Meta) — push ingress;
  * the mailbox poll sweep (BYO Outlook, §3.1.6a) — pull ingress through the
    tenant's own authenticated Graph session.

Whatever the transport, the same admission gates run (verify → resolve → pre-budget
admission → normalize) and the same reply path pushes Luciel's answer back through
the channel adapter — so a polled email cannot dodge rate limits, opt-out handling,
or delivery honesty just because no webhook was involved.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels import outbound, pipeline
from app.channels.base import ChannelAdapter, InboundMessage, InboundRejected
from app.channels.runtime_entry import RuntimeReply, run_inbound_message
from app.db.session import tenant_session, untenanted_session

logger = logging.getLogger(__name__)


async def run_and_reply(msg: InboundMessage) -> str:
    """Run an admitted InboundMessage through the runtime and deliver Luciel's answer.

    Returns the reply text; ``run_and_reply_full`` returns the whole RuntimeReply for
    an edge that needs more than the text (the SMS edge keys its first-message
    notice on ``is_new_session``).
    """
    return (await run_and_reply_full(msg)).reply_text


async def run_and_reply_full(msg: InboundMessage) -> RuntimeReply:
    """Run an admitted InboundMessage through the runtime and deliver Luciel's answer.

    SMS rides back in the TwiML response the caller builds, so it is left alone here.
    Every other channel's provider needs an out-of-band push, so the reply is sent
    through the channel adapter before the caller acks — otherwise the lead gets
    silence.
    """
    async with tenant_session(msg.admin_id) as s:
        reply = await run_inbound_message(s, msg)
        if reply.reply_text and msg.channel not in outbound.INLINE_REPLY_CHANNELS:
            result = await outbound.send_outbound_reply(
                s,
                admin_id=msg.admin_id,
                instance_id=msg.instance_id,
                channel=msg.channel,
                # Reply to the identifier the lead wrote in from (§3.4.9), never a
                # stored contact on another channel.
                to=msg.customer_identifier,
                body=reply.reply_text,
                subject=msg.channel_metadata.get("subject"),
                sender_id=(
                    msg.channel_metadata.get("destination_id")
                    or msg.channel_metadata.get("phone_number_id")
                ),
                # BYO-mailbox threading: the Graph id of the message being answered,
                # when the ingress knew it. Senders without a reply concept ignore it.
                in_reply_to=msg.channel_metadata.get("graph_message_id"),
            )
            if not result.delivered:
                # An AI reply that did not reach the customer is an incident-shaped
                # fact, not a return value to discard — the transcript records what
                # Luciel SAID; this records whether anyone HEARD it. No recipient
                # identifiers in the line (§3.8.3): channel + reason only.
                logger.warning(
                    "inbound_reply_undelivered channel=%s detail=%s",
                    msg.channel,
                    result.detail,
                )
                await _escalate_undelivered_reply(s, msg, reply, result.detail)
        return reply


async def _escalate_undelivered_reply(
    s: AsyncSession, msg: InboundMessage, reply: RuntimeReply, detail: str | None
) -> None:
    """A reply Luciel wrote that the channel could not deliver becomes an escalation
    (audit F077): the owner is told a customer is waiting on a channel that cannot
    answer, instead of a warning line nobody reads. Once per session."""
    from app.runtime.orchestrator import record_platform_escalation  # noqa: PLC0415

    if not reply.session_id:
        return
    await record_platform_escalation(
        s,
        admin_id=msg.admin_id,
        instance_id=msg.instance_id,
        session_id=uuid.UUID(reply.session_id),
        signal="reply_undeliverable",
        detail=detail,
    )


async def dispatch_inbound(adapter: ChannelAdapter, req: dict[str, Any]) -> str | None:
    """Run the inbound edge + runtime; return reply text, or None if not routed."""
    reply = await dispatch_inbound_reply(adapter, req)
    return reply.reply_text if reply is not None else None


async def dispatch_inbound_reply(
    adapter: ChannelAdapter, req: dict[str, Any]
) -> RuntimeReply | None:
    """Run the inbound edge + runtime; return the RuntimeReply, or None if not routed.

    Signature/resolution failures are rejected + logged (never raised to the provider
    as a 5xx — a rejected inbound is a clean no-op ack so the provider does not retry
    a spoofed/garbled request forever)."""
    async with untenanted_session() as s:
        try:
            outcome = await pipeline.ingest(adapter, req, s)
        except InboundRejected as rej:
            await pipeline.record_inbound_rejection(
                s, reason=rej.reason, channel=adapter.channel_id
            )
            return None
    if isinstance(outcome, InboundMessage):
        return await run_and_reply_full(outcome)
    if isinstance(outcome, pipeline.OptOutHandled):
        await _confirm_opt_keyword(outcome)
        return None
    if isinstance(outcome, pipeline.ContactReply):
        await _record_contact_reply(outcome)
        return None
    # AdmissionDenied / InboundRejected → no runtime reply.
    return None


_KEYWORD_REPLIES = {
    "stop": (
        "You've been unsubscribed and won't receive further messages here. "
        "Reply START to opt back in."
    ),
    "start": "You're opted back in. Reply STOP at any time to unsubscribe.",
    "help": "This is an AI assistant. Reply STOP to unsubscribe or START to opt back in.",
}


async def _confirm_opt_keyword(outcome: pipeline.OptOutHandled) -> None:
    """Send the canonical STOP/START/HELP confirmation on channels with no carrier to
    do it (audit F074): the SMS carrier answers its own keywords, WhatsApp/Messenger/
    Instagram/email get the platform's."""
    if outcome.channel in outbound.INLINE_REPLY_CHANNELS or outcome.channel == "voice":
        return
    text = _KEYWORD_REPLIES.get(outcome.keyword)
    if not text or not outcome.customer_identifier:
        return
    if outcome.admin_id is None or outcome.instance_id is None:
        return
    async with tenant_session(outcome.admin_id) as s:
        await outbound.send_outbound_reply(
            s,
            admin_id=outcome.admin_id,
            instance_id=outcome.instance_id,
            channel=outcome.channel,
            to=outcome.customer_identifier,
            body=text,
            subject=outcome.subject,
            sender_id=outcome.sender_id,
        )


async def _record_contact_reply(outcome: pipeline.ContactReply) -> None:
    """An escalation CONTACT wrote back on the tenant's number (audit F051): that is the
    owner's side of the conversation, not a lead — no session, no runtime, no billing.
    Recorded so the dashboard's escalation trail shows the acknowledgement."""
    from app.audit import log as audit  # noqa: PLC0415
    from app.models.audit import ActorType  # noqa: PLC0415

    async with tenant_session(outcome.admin_id) as s:
        await audit.emit(
            s,
            admin_id=outcome.admin_id,
            instance_id=outcome.instance_id,
            actor_type=ActorType.admin_user,
            event_type="escalation_acked",
            payload={
                "channel": outcome.channel,
                "contact_role": outcome.contact_role,
                "reply_length": len(outcome.body),
            },
        )
