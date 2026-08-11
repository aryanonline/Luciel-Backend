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
from typing import Any

from app.channels import outbound, pipeline
from app.channels.base import ChannelAdapter, InboundMessage, InboundRejected
from app.channels.runtime_entry import run_inbound_message
from app.db.session import tenant_session, untenanted_session

logger = logging.getLogger(__name__)


async def run_and_reply(msg: InboundMessage) -> str:
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
        return reply.reply_text


async def dispatch_inbound(adapter: ChannelAdapter, req: dict[str, Any]) -> str | None:
    """Run the inbound edge + runtime; return reply text, or None if not routed.

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
        return await run_and_reply(outcome)  # noqa: RET504
    # AdmissionDenied / OptOutHandled / InboundRejected → no runtime reply.
    return None
