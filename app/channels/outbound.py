"""Outbound dispatch for admin-authored replies (Architecture §3.4.12 / §11.5-11.7).

When an admin takes over a conversation and types a reply, the message must reach the
customer on the SAME channel they wrote in on — persisting a transcript row alone would
leave the customer waiting forever. This module is the one place that turns
"(channel, recipient, text)" into a real send through the existing channel adapters.

Push vs poll (the honest limitation): SMS / WhatsApp / Instagram-Messenger / email all
have a provider push API, so the reply is delivered. The **widget** has no push
transport — the browser holds the connection, not us — so a widget reply is persisted
and picked up by the widget's next history fetch. Callers must surface that difference
rather than claim delivery (``delivered=False`` + ``detail="widget_poll_only"``).

Every send goes through the adapter that already owns the provider quirks; in mock/CI
the adapters bind their Null senders, so this module makes no network call.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import OutboundMessage
from app.models.connection import InstanceConnection

# Channels the customer reads by polling us; we cannot push to them (§11.7).
POLL_ONLY_CHANNELS = frozenset({"widget"})


@dataclass(frozen=True)
class OutboundResult:
    """Outcome of one outbound attempt. ``delivered`` is the honest wire signal."""

    channel: str
    delivered: bool
    provider_message_id: str | None = None
    # Machine-readable reason when not delivered: widget_poll_only | no_recipient |
    # channel_not_provisioned | unsupported_channel | the adapter's own failure detail.
    detail: str | None = None


async def _connection_config(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    connection_type: str,
) -> dict[str, object]:
    conn: InstanceConnection | None = (
        await session.execute(
            select(InstanceConnection).where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.instance_id == instance_id,
                InstanceConnection.connection_type == connection_type,
                InstanceConnection.status != "revoked",
            )
        )
    ).scalar_one_or_none()
    if conn is None or not conn.non_secret_config:
        return {}
    return dict(conn.non_secret_config)


async def send_human_reply(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    channel: str,
    to: str | None,
    body: str,
) -> OutboundResult:
    """Push one admin-authored reply to the customer on ``channel`` (§3.4.12).

    Never raises for a delivery problem — an undeliverable reply is reported as data so
    the dashboard can tell the admin the truth instead of failing the whole request
    after the transcript row is already written.
    """
    if channel in POLL_ONLY_CHANNELS:
        return OutboundResult(channel=channel, delivered=False, detail="widget_poll_only")
    if not to:
        return OutboundResult(channel=channel, delivered=False, detail="no_recipient")

    if channel in ("sms", "voice"):
        from app.channels.sms_adapter import SmsAdapter

        cfg = await _connection_config(
            session, admin_id=admin_id, instance_id=instance_id, connection_type="sms_sender"
        )
        from_number = cfg.get("destination")
        if not isinstance(from_number, str) or not from_number:
            return OutboundResult(
                channel=channel, delivered=False, detail="channel_not_provisioned"
            )
        receipt = await SmsAdapter().send(
            OutboundMessage(channel="sms", to=to, body=body, metadata={"from_number": from_number})
        )
    elif channel == "email":
        from app.channels.email_adapter import EmailAdapter
        from app.connections.service import resolve_email_address

        address = await resolve_email_address(session, admin_id, instance_id)
        if not address:
            return OutboundResult(
                channel=channel, delivered=False, detail="channel_not_provisioned"
            )
        receipt = await EmailAdapter(_PlatformSesSender(), _NoInboundVerifier()).send(
            OutboundMessage(
                channel="email",
                to=to,
                body=body,
                metadata={"sender_identity": address, "subject": "Re: your inquiry"},
            )
        )
    elif channel in ("whatsapp", "instagram_messenger"):
        from app.channels.whatsapp_adapter import WhatsAppAdapter

        cfg = await _connection_config(
            session, admin_id=admin_id, instance_id=instance_id, connection_type="channel_auth"
        )
        sender_id = cfg.get("destination")
        if not isinstance(sender_id, str) or not sender_id:
            return OutboundResult(
                channel=channel, delivered=False, detail="channel_not_provisioned"
            )
        receipt = await WhatsAppAdapter().send(
            OutboundMessage(
                channel=channel, to=to, body=body, metadata={"phone_number_id": sender_id}
            )
        )
    else:
        return OutboundResult(channel=channel, delivered=False, detail="unsupported_channel")

    return OutboundResult(
        channel=channel,
        delivered=receipt.accepted,
        provider_message_id=receipt.provider_message_id,
        detail=receipt.detail,
    )


class _NoInboundVerifier:
    """The EmailAdapter takes an SNS verifier for its INBOUND half; the send path never
    calls it. Refuse loudly if it is ever reached, rather than pretending to verify."""

    def verify(self, message: dict[str, Any]) -> bool:
        raise RuntimeError("outbound-only EmailAdapter cannot verify inbound SNS")


class _PlatformSesSender:
    """Adapt the platform SES sender to the channel EmailAdapter's send_email shape.

    Binds SES in live mode and the in-memory Null sender in mock/CI (the same
    integration-mode seam the notification path uses)."""

    async def send_email(self, sender: str, to: str, subject: str, body: str) -> str | None:
        from app.notifications.email_sender import build_email_sender

        return await build_email_sender().send(to=to, subject=subject, body_text=body)
