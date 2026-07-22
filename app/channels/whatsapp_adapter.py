"""WhatsApp channel adapter (Architecture §3.1.2, §3.1.3, §3.8.1 — Meta).

WhatsApp is its own channel (`channel_id = whatsapp`, its own adapter). Inbound is a
Meta Graph webhook whose authenticity is verified via X-Hub-Signature-256 (HMAC-SHA256
over the raw body) BEFORE the tenant lookup (§3.1.3/§3.7.2). The destination WhatsApp
Business phone-number ID resolves to the tenant (§3.1.3). Outbound goes through the
Meta Graph API via an injected sender (fake/Null in mock/CI — no network — mirroring
the SMS/email adapters).

The WhatsApp Business API binds a WhatsApp Business account to a phone number; number
eligibility/migration is handled at channel-connect (§3.1.2), not here. This adapter
only normalizes inbound and sends outbound.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from app.channels.base import (
    DeliveryReceipt,
    InboundMessage,
    InboundRejected,
    InstanceContext,
    OutboundMessage,
)
from app.channels.signatures import verify_hmac_shared_secret


class MetaSender(Protocol):
    async def send_message(
        self, *, channel: str, sender_id: str, recipient_id: str, body: str
    ) -> str | None:
        """Send via the Meta Graph API; return the provider message id, or None."""
        ...


class NullMetaSender:
    """Mock/CI sender — records sends in-memory, no network."""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    async def send_message(
        self, *, channel: str, sender_id: str, recipient_id: str, body: str
    ) -> str | None:
        self.sent.append({"channel": channel, "from": sender_id, "to": recipient_id, "body": body})
        return f"null-meta-{len(self.sent)}"


def _first_message(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the first inbound message out of Meta's nested webhook envelope.

    Shape: entry[].changes[].value.messages[]; value.metadata.phone_number_id is the
    tenant's WhatsApp Business number ID (the resolution key).
    """
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages") or []
            if messages:
                return {"value": value, "message": messages[0]}
    return None


class WhatsAppAdapter:
    channel_id = "whatsapp"

    def __init__(self, sender: MetaSender | None = None) -> None:
        self._sender = sender or _build_meta_sender()

    async def verify_inbound(self, request: dict[str, Any]) -> InstanceContext:
        # Signature FIRST (§3.1.3): HMAC-SHA256 over the raw body via X-Hub-Signature-256.
        app_secret = request["app_secret"]
        raw_body: bytes = request["raw_body"]
        signature = request.get("x_hub_signature_256", "")
        if not verify_hmac_shared_secret(app_secret, raw_body, signature):
            raise InboundRejected("meta_signature_invalid")

        parsed = _first_message(request["payload"])
        if parsed is None:
            raise InboundRejected("whatsapp_no_message")
        # Destination = the tenant's WhatsApp Business phone-number ID.
        phone_number_id = parsed["value"].get("metadata", {}).get("phone_number_id")
        if not phone_number_id:
            raise InboundRejected("whatsapp_no_destination")
        resolve = request["resolve"]
        ctx: InstanceContext = await resolve("channel_auth", phone_number_id)
        return ctx

    async def receive(self, request: dict[str, Any], ctx: InstanceContext) -> InboundMessage:
        parsed = _first_message(request["payload"])
        if parsed is None:
            # verify_inbound already guaranteed a message; a None here means the payload
            # changed between verify and receive. Reject rather than crash.
            raise InboundRejected("whatsapp_no_message")
        msg = parsed["message"]
        # WhatsApp text lives at message.text.body; the sender is message.from (E.164).
        body = (msg.get("text") or {}).get("body", "")
        return InboundMessage(
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            channel="whatsapp",
            session_id=None,
            customer_identifier=msg.get("from", ""),
            body=body,
            channel_metadata={
                "wa_message_id": msg.get("id"),
                "phone_number_id": parsed["value"].get("metadata", {}).get("phone_number_id"),
            },
            received_at=datetime.now(UTC),
        )

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        sender_id = message.metadata.get("phone_number_id", "")
        try:
            mid = await self._sender.send_message(
                channel="whatsapp",
                sender_id=sender_id,
                recipient_id=message.to,
                body=message.body,
            )
        except Exception as exc:  # noqa: BLE001
            return DeliveryReceipt(accepted=False, detail=f"meta_error:{type(exc).__name__}")
        if mid is None:
            return DeliveryReceipt(accepted=False, detail="meta_rejected")
        return DeliveryReceipt(accepted=True, provider_message_id=mid)


def _build_meta_sender() -> MetaSender:
    """Live Meta Graph sender when configured, else Null (mock/CI). Imported lazily
    to avoid a hard dependency in CI."""
    from app.channels.meta_sender import build_meta_sender

    return build_meta_sender()
