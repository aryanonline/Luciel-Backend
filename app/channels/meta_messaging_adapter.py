"""Instagram DM + Facebook Messenger adapter (Architecture §3.1.2, §3.1.3).

Instagram DM and Messenger SHARE one adapter and one `channel_id = instagram_messenger`
(canonical §3.1.2): they ride the same Meta Graph/Messenger API surface and the platform
routes/rate-limits/configures them as a single channel. Inbound is a Meta webhook whose
X-Hub-Signature-256 is verified BEFORE tenant lookup (§3.1.3/§3.7.2); the destination
Instagram-account ID or Facebook-page ID resolves to the tenant. These channels bind to
the admin's Meta page/account, NOT a phone number (§3.1.3). Outbound goes through the
shared Meta Graph sender (fake/Null in mock/CI).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.channels.base import (
    DeliveryReceipt,
    InboundMessage,
    InboundRejected,
    InstanceContext,
    OutboundMessage,
)
from app.channels.signatures import verify_hmac_shared_secret
from app.channels.whatsapp_adapter import MetaSender


def _first_messaging_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the first inbound messaging event from Meta's Messenger/IG webhook.

    Shape: entry[].messaging[] (Messenger) — each entry carries the recipient (the
    tenant's page/IG-account id) and the sender (the lead's PSID/IGSID).
    """
    for entry in payload.get("entry", []):
        messaging = entry.get("messaging") or entry.get("standby") or []
        for event in messaging:
            if event.get("message"):
                return {"entry": entry, "event": event}
    return None


class MetaMessagingAdapter:
    channel_id = "instagram_messenger"

    def __init__(self, sender: MetaSender | None = None) -> None:
        self._sender = sender or _build_meta_sender()

    async def verify_inbound(self, request: dict[str, Any]) -> InstanceContext:
        # Signature FIRST (§3.1.3): X-Hub-Signature-256 HMAC-SHA256 over the raw body.
        app_secret = request["app_secret"]
        raw_body: bytes = request["raw_body"]
        signature = request.get("x_hub_signature_256", "")
        if not verify_hmac_shared_secret(app_secret, raw_body, signature):
            raise InboundRejected("meta_signature_invalid")

        parsed = _first_messaging_event(request["payload"])
        if parsed is None:
            raise InboundRejected("meta_no_message")
        # Destination = tenant's page/IG-account id (entry.id), the resolution key.
        destination_id = parsed["entry"].get("id")
        if not destination_id:
            raise InboundRejected("meta_no_destination")
        resolve = request["resolve"]
        ctx: InstanceContext = await resolve("channel_auth", destination_id)
        return ctx

    async def receive(self, request: dict[str, Any], ctx: InstanceContext) -> InboundMessage:
        parsed = _first_messaging_event(request["payload"])
        if parsed is None:
            # verify_inbound already guaranteed a message event; a None here means the
            # payload changed between verify and receive. Reject rather than crash.
            raise InboundRejected("meta_no_message")
        event = parsed["event"]
        body = (event.get("message") or {}).get("text", "")
        return InboundMessage(
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            channel="instagram_messenger",
            session_id=None,
            customer_identifier=(event.get("sender") or {}).get("id", ""),
            body=body,
            channel_metadata={
                "mid": (event.get("message") or {}).get("mid"),
                "destination_id": parsed["entry"].get("id"),
            },
            received_at=datetime.now(UTC),
        )

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        sender_id = message.metadata.get("destination_id", "")
        try:
            mid = await self._sender.send_message(
                channel="instagram_messenger",
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
    from app.channels.meta_sender import build_meta_sender

    return build_meta_sender()
