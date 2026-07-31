"""Outbound reply dispatch for the non-widget channels (Architecture §3.4.12 / §3.1.2).

One place that turns "(channel, recipient, text)" into a real send through the existing
channel adapters, used by BOTH replies that a customer can receive:

- Luciel's own answer on an inbound channel whose provider needs an out-of-band push
  (email, WhatsApp, Instagram DM / Messenger). SMS is the exception — Twilio delivers
  Luciel's answer in the TwiML response to the webhook, so it must NOT be pushed twice.
- An admin's ``human_agent`` reply during live takeover (§3.4.12), which arrives long
  after the inbound request is gone and therefore has no response body to ride on.

Both paths reply to the identifier the lead actually wrote in from (§3.4.9) — the verified
From address for email, the platform-scoped IG/page-scoped id for Meta. A stored contact is
never used as an outbound destination on a different channel: those identities merge only
when the lead volunteers the linking contact info in-conversation.

Push vs poll (the honest limitation): the **widget** has no push transport — the browser
holds the connection, not us — so a widget reply is persisted and picked up by the widget's
next history fetch. Callers must surface that difference rather than claim delivery
(``delivered=False`` + ``detail="widget_poll_only"``).

Every send goes through the adapter that already owns the provider quirks; in mock/CI the
adapters bind their Null senders, so this module makes no network call.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import DeliveryReceipt, OutboundMessage
from app.notifications.email_sender import EmailSender as EmailSenderProtocol

# Channels the customer reads by polling us; we cannot push to them (§11.7).
POLL_ONLY_CHANNELS = frozenset({"widget"})

# Twilio delivers Luciel's answer inline via TwiML, so the runtime reply on this channel is
# already on the wire by the time the webhook returns (§3.1.4).
INLINE_REPLY_CHANNELS = frozenset({"sms"})

DEFAULT_EMAIL_SUBJECT = "Re: your inquiry"


@dataclass(frozen=True)
class OutboundResult:
    """Outcome of one outbound attempt. ``delivered`` is the honest wire signal."""

    channel: str
    delivered: bool
    provider_message_id: str | None = None
    # Machine-readable reason when not delivered: widget_poll_only | no_recipient |
    # channel_not_provisioned | unsupported_channel | the adapter's own failure detail.
    detail: str | None = None


async def send_outbound_reply(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    channel: str,
    to: str | None,
    body: str,
    subject: str | None = None,
    sender_id: str | None = None,
) -> OutboundResult:
    """Push one reply to the customer on ``channel``.

    ``subject`` threads an email reply onto the lead's own subject line; ``sender_id`` is
    the tenant's channel identity carried straight off the inbound message (Meta page/IG
    id, WhatsApp phone-number id) and falls back to the provisioned connection when the
    caller has no inbound context — i.e. on the takeover path.

    Never raises for a delivery problem — an undeliverable reply is reported as data so
    the caller can tell the admin the truth instead of failing the whole request after the
    transcript row is already written.
    """
    if channel in POLL_ONLY_CHANNELS:
        return OutboundResult(channel=channel, delivered=False, detail="widget_poll_only")
    if not to:
        return OutboundResult(channel=channel, delivered=False, detail="no_recipient")

    if channel in ("sms", "voice"):
        receipt = await _send_sms(session, admin_id, instance_id, to=to, body=body)
    elif channel == "email":
        receipt = await _send_email(
            session, admin_id, instance_id, to=to, body=body, subject=subject
        )
    elif channel in ("whatsapp", "instagram_messenger"):
        receipt = await _send_meta(
            session, admin_id, instance_id, channel=channel, to=to, body=body, sender_id=sender_id
        )
    else:
        return OutboundResult(channel=channel, delivered=False, detail="unsupported_channel")

    return OutboundResult(
        channel=channel,
        delivered=receipt.accepted,
        provider_message_id=receipt.provider_message_id,
        detail=receipt.detail,
    )


_NOT_PROVISIONED = DeliveryReceipt(accepted=False, detail="channel_not_provisioned")


def _sender_not_provisioned(seam: str) -> DeliveryReceipt:
    """A live deployment whose delivery seam resolved to a Null sender.

    Nothing can leave the system, so the reply is reported as an actionable failure
    rather than a delivery — the same posture ``email_notifier``/``sms_notifier`` take.
    """
    return DeliveryReceipt(accepted=False, detail=f"{seam}_sender_not_provisioned")


async def _send_sms(
    session: AsyncSession,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    to: str,
    body: str,
) -> DeliveryReceipt:
    from app.channels.sms_adapter import SmsAdapter
    from app.channels.tenant_twilio import build_tenant_sms_sender
    from app.connections.service import resolve_sms_number
    from app.runtime.integration_status import null_in_live

    from_number = await resolve_sms_number(session, admin_id, instance_id)
    if not from_number:
        return _NOT_PROVISIONED
    # BYO (§3.1.4): send through the tenant's OWN Twilio, never the platform's.
    sender = await build_tenant_sms_sender(session, admin_id, instance_id)
    if null_in_live(sender):
        return _sender_not_provisioned("sms")
    return await SmsAdapter(sender).send(
        OutboundMessage(channel="sms", to=to, body=body, metadata={"from_number": from_number})
    )


async def _send_email(
    session: AsyncSession,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    to: str,
    body: str,
    subject: str | None,
) -> DeliveryReceipt:
    from app.channels.email_adapter import EmailAdapter
    from app.connections.service import resolve_email_address
    from app.notifications.email_sender import build_email_sender
    from app.runtime.integration_status import null_in_live

    address = await resolve_email_address(session, admin_id, instance_id)
    if not address:
        return _NOT_PROVISIONED
    email_sender = build_email_sender()
    if null_in_live(email_sender):
        return _sender_not_provisioned("email")
    return await EmailAdapter(_PlatformSesSender(email_sender), _NoInboundVerifier()).send(
        OutboundMessage(
            channel="email",
            to=to,
            body=body,
            # The tenant's provisioned address is the FROM identity (§3.1.6a) — a reply
            # from a platform address would be unreplyable and unrecognizable.
            metadata={"sender_identity": address, "subject": _reply_subject(subject)},
        )
    )


async def _send_meta(
    session: AsyncSession,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    channel: str,
    to: str,
    body: str,
    sender_id: str | None,
) -> DeliveryReceipt:
    from app.channels.meta_sender import build_tenant_meta_sender
    from app.connections.service import meta_grant_for_destination, resolve_meta_destination
    from app.runtime.integration_status import null_in_live

    # Per-channel (§3.8.1): a WhatsApp number id, an IG account id and a Messenger page id
    # are bound side by side, so the reply must pick its own.
    identity = sender_id or await resolve_meta_destination(
        session, admin_id, instance_id, channel=channel
    )
    if not identity:
        return _NOT_PROVISIONED
    # The token comes off the connection that OWNS this asset (P1-1): the asset we send AS
    # and the grant we send WITH must belong to one tenant — and, since the Instagram Login
    # split, to one app.
    grant = await meta_grant_for_destination(
        session, admin_id, instance_id, channel=channel, destination=identity
    )
    meta_sender = await build_tenant_meta_sender(
        session, admin_id, instance_id, connection_type=grant
    )
    if null_in_live(meta_sender):
        return _sender_not_provisioned("meta")
    if channel == "whatsapp":
        from app.channels.whatsapp_adapter import WhatsAppAdapter

        # WhatsApp Cloud API sends from a phone-number id.
        return await WhatsAppAdapter(meta_sender).send(
            OutboundMessage(
                channel=channel, to=to, body=body, metadata={"phone_number_id": identity}
            )
        )
    from app.channels.meta_messaging_adapter import MetaMessagingAdapter

    # IG DM / Messenger send from the IG-account / page id, on the Messenger send API —
    # a different Graph shape from WhatsApp, hence a different adapter (§3.1.2).
    return await MetaMessagingAdapter(meta_sender).send(
        OutboundMessage(channel=channel, to=to, body=body, metadata={"destination_id": identity})
    )


def _reply_subject(subject: str | None) -> str:
    if not subject:
        return DEFAULT_EMAIL_SUBJECT
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


class _NoInboundVerifier:
    """The EmailAdapter takes an SNS verifier for its INBOUND half; the send path never
    calls it. Refuse loudly if it is ever reached, rather than pretending to verify."""

    def verify(self, message: dict[str, Any]) -> bool:
        raise RuntimeError("outbound-only EmailAdapter cannot verify inbound SNS")


class _PlatformSesSender:
    """Adapt the platform SES sender to the channel EmailAdapter's send_email shape."""

    def __init__(self, sender: EmailSenderProtocol) -> None:
        self._sender = sender

    async def send_email(self, sender: str, to: str, subject: str, body: str) -> str | None:
        return await self._sender.send(to=to, subject=subject, body_text=body, from_address=sender)
