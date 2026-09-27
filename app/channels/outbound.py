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

# The Meta messaging surfaces (§3.1.2 split ids + the legacy combined id): the reply is
# sent AS a tenant surface (page / IG account / WhatsApp phone-number id) TO a
# platform-scoped customer id, through the grant that owns that surface.
META_CHANNELS = frozenset({"whatsapp", "messenger", "instagram", "instagram_messenger"})

DEFAULT_EMAIL_SUBJECT = "Re: your inquiry"

#: Who wrote the reply (round 7 WP-3b, item 3). Mirrors the transcript roles.
AUTHOR_LUCIEL = "luciel"
AUTHOR_HUMAN_AGENT = "human_agent"


@dataclass(frozen=True)
class OutboundResult:
    """Outcome of one outbound attempt. ``delivered`` is the honest wire signal."""

    channel: str
    delivered: bool
    provider_message_id: str | None = None
    # Machine-readable reason when not delivered: widget_poll_only | no_recipient |
    # channel_not_provisioned | unsupported_channel | sms_sender_not_operable |
    # email_sender_not_connected | mailbox_reconnect_needed | meta_reconnect_needed |
    # the adapter's own failure detail.
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
    in_reply_to: str | None = None,
    author_role: str = AUTHOR_LUCIEL,
) -> OutboundResult:
    """Push one reply to the customer on ``channel``.

    ``subject`` threads an email reply onto the lead's own subject line; ``sender_id`` is
    the tenant's channel identity carried straight off the inbound message (Meta page/IG
    id, WhatsApp phone-number id) — on the takeover path it is the one persisted on the
    session at ingress — and falls back to the provisioned connection when the caller
    has neither. ``in_reply_to`` is the provider-side id of the inbound message being
    answered (the Graph message id on a BYO-mailbox reply, the RFC 5322 Message-ID on
    the legacy SES path), for true threading; senders without a reply concept ignore it.

    ``author_role`` says WHO wrote the text — ``luciel`` (the default) or
    ``human_agent`` — and decides which email footer the reply carries (round 7 WP-3b,
    item 3): a person's reply is never signed as written by an AI assistant.

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
            session,
            admin_id,
            instance_id,
            to=to,
            body=body,
            subject=subject,
            in_reply_to=in_reply_to,
            author_role=author_role,
        )
    elif channel in META_CHANNELS:
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
    # The number's CONNECTION must be operable before anything goes out on it (audit
    # F070): resolve_sms_number reads any non-revoked row, so a number the probe had
    # flagged (hosting required / action needed / expired / dormant) still "sent" and
    # Twilio dropped it. Pending carrier registration IS operable — 10DLC is the
    # tenant's business (§3.1.4) and the number can send meanwhile.
    row = await _connection_row(session, admin_id, instance_id, "sms_sender")
    if row is None or row.status not in SMS_OPERABLE_STATUSES:
        return DeliveryReceipt(accepted=False, detail="sms_sender_not_operable")
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
    in_reply_to: str | None = None,
    author_role: str = AUTHOR_LUCIEL,
) -> DeliveryReceipt:
    from app.channels.email_adapter import EmailAdapter
    from app.connections.service import resolve_email_address
    from app.notifications.email_sender import build_email_sender
    from app.runtime.integration_status import null_in_live

    # Every reply carries the opt-out / privacy footer (F073); only Luciel's own
    # carries the AI-identity line (round 7 WP-3b, item 3).
    body = await _with_email_footer(session, admin_id, instance_id, body, author_role=author_role)
    # BYO mailbox (§3.1.6a): when the tenant's email sender IS their Outlook mailbox,
    # the reply goes out through THEIR Graph credential — and only through it. There
    # is deliberately no SES fallback on this branch: falling back would swap the
    # FROM identity behind the owner's back, the mirror image of "never silently
    # replaces a working sender".
    conn = await _email_sender_row(session, admin_id, instance_id)
    if conn is not None and conn.provider == "outlook":
        from app.channels.outlook_sender import build_tenant_outlook_sender

        if conn.status != "connected":
            return DeliveryReceipt(accepted=False, detail="mailbox_reconnect_needed")
        sender = await build_tenant_outlook_sender(session, admin_id, instance_id)
        if null_in_live(sender):
            # Connected on paper but the token could not be made fresh — the health
            # sweep will mark it expired within its cadence; the reply must not wait
            # on that to tell the truth.
            return DeliveryReceipt(accepted=False, detail="mailbox_reconnect_needed")
        accepted = await sender.send_mail(
            to=to, subject=_reply_subject(subject), body=body, in_reply_to=in_reply_to
        )
        # Graph's 202 carries no message id — report none rather than invent one.
        return DeliveryReceipt(
            accepted=accepted,
            provider_message_id=None,
            detail=None if accepted else "mailbox_send_failed",
        )

    address = await resolve_email_address(session, admin_id, instance_id)
    if not address:
        return _NOT_PROVISIONED
    if conn is not None and conn.status != "connected":
        # The legacy SES row honours the same health gate the mailbox branch does (F070).
        return DeliveryReceipt(accepted=False, detail="email_sender_not_connected")
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
            # ``in_reply_to`` is the customer's RFC 5322 Message-ID: the adapter turns
            # it into In-Reply-To / References so the legacy SES reply threads too.
            metadata={
                "sender_identity": address,
                "subject": _reply_subject(subject),
                "in_reply_to": in_reply_to,
            },
        )
    )


async def send_outbound_email(
    session: AsyncSession,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    *,
    to: str,
    body: str,
    subject: str | None = None,
) -> DeliveryReceipt:
    """Send one email AS the tenant (round 6 WP-H, F141): the BYO mailbox when one is
    connected, else the legacy provisioned address; no silent SES fallback and the
    footer every reply carries. The ``send_email`` tool and the channel-switch path
    use this so an email Luciel starts is indistinguishable, to the customer, from
    one it replies with."""
    return await _send_email(session, admin_id, instance_id, to=to, body=body, subject=subject)


async def _email_sender_row(
    session: AsyncSession, admin_id: uuid.UUID, instance_id: uuid.UUID
) -> Any:  # InstanceConnection | None (lazy import keeps module import light)
    return await _connection_row(session, admin_id, instance_id, "email_sender")


async def _connection_row(
    session: AsyncSession, admin_id: uuid.UUID, instance_id: uuid.UUID, connection_type: str
) -> Any:
    from sqlalchemy import select

    from app.models.connection import InstanceConnection

    return (
        await session.execute(
            select(InstanceConnection).where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.instance_id == instance_id,
                InstanceConnection.connection_type == connection_type,
                InstanceConnection.status != "revoked",
            )
        )
    ).scalar_one_or_none()


#: sms_sender statuses on which a text may leave (phone_lifecycle): attested-live, or
#: operable-and-pending the tenant's own carrier registration.
SMS_OPERABLE_STATUSES = frozenset({"connected", "pending_carrier_registration"})

EMAIL_FOOTER_MARKER = "This message was written by an AI assistant"
#: The line both footers share — the opt-out promise every customer email carries
#: (Legal §A2). Its presence is what makes the human footer idempotent.
EMAIL_OPT_OUT_LINE = "Reply STOP to opt out of further messages."


async def _with_email_footer(
    session: AsyncSession,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    body: str,
    *,
    author_role: str = AUTHOR_LUCIEL,
) -> str:
    """Every email that leaves the platform carries the opt-out + privacy footer
    (§3.4.16, Legal §A2; audit F073: replies went out bare). Idempotent.

    Luciel's own reply is signed as written by an AI assistant (the disclosure that
    must be present on every channel). A ``human_agent`` reply — the owner answering
    during a live takeover — is signed by the business and NEVER carries that line
    (round 7 WP-3b, item 3): an AI disclosure under a person's words is the mirror
    lie of representing a human as answering."""
    if EMAIL_FOOTER_MARKER in body or EMAIL_OPT_OUT_LINE in body:
        return body
    from sqlalchemy import select

    from app.channels.runtime_entry import disclosure_business_name
    from app.config import get_settings
    from app.models.instance import Instance

    inst = (
        await session.execute(select(Instance).where(Instance.id == instance_id))
    ).scalar_one_or_none()
    business = disclosure_business_name(inst) if inst is not None else "this business"
    privacy = f"{get_settings().app_base_url.rstrip('/')}/legal/privacy"
    if author_role == AUTHOR_HUMAN_AGENT:
        signature = f"Sent by {business}."
    else:
        signature = f"{EMAIL_FOOTER_MARKER} on behalf of {business}."
    footer = f"\n\n--\n{signature} {EMAIL_OPT_OUT_LINE} Privacy: {privacy}"
    return f"{body.rstrip()}{footer}"


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
    # The owning grant must be healthy before anything goes out on it (round 7 WP-3b,
    # item 7; mirrors the SMS and mailbox gates): an expired / errored / never-bound
    # row still "sent" through a token Graph then rejected, and the failure collapsed
    # to a generic send_failed with "try again" copy. The truth is that the owner has
    # to reconnect Meta — say so in the wire vocabulary.
    row = await _connection_row(session, admin_id, instance_id, grant)
    if row is not None and row.status != "connected":
        return DeliveryReceipt(accepted=False, detail="meta_reconnect_needed")
    meta_sender = await build_tenant_meta_sender(
        session, admin_id, instance_id, connection_type=grant
    )
    if null_in_live(meta_sender):
        # Live, but no grant / no token could be loaded: nothing can leave. That is a
        # reconnect, not a transient failure.
        return DeliveryReceipt(accepted=False, detail="meta_reconnect_needed")
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
    # a different Graph shape from WhatsApp, hence a different adapter (§3.1.2). The
    # split channel id is threaded through so the send is attributed to the real channel.
    return await MetaMessagingAdapter(meta_sender, channel=channel).send(
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
    """Adapt the platform SES sender to the channel EmailAdapter's send_email shape.

    Threading headers are forwarded only when the platform sender's ``send`` accepts a
    ``headers`` keyword (round 7 WP-3b, item 2); a sender without one still sends —
    the mail lands unthreaded, which is a degraded reply, not a lost one."""

    def __init__(self, sender: EmailSenderProtocol) -> None:
        self._sender = sender

    async def send_email(
        self,
        sender: str,
        to: str,
        subject: str,
        body: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> str | None:
        kwargs: dict[str, Any] = {
            "to": to,
            "subject": subject,
            "body_text": body,
            "from_address": sender,
        }
        if headers and _accepts_headers(self._sender.send):
            kwargs["headers"] = headers
        return await self._sender.send(**kwargs)


def _accepts_headers(send: Any) -> bool:
    import inspect

    try:
        params = inspect.signature(send).parameters
    except (TypeError, ValueError):
        return False
    return "headers" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
