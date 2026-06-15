"""SMS channel adapter — Twilio inbound + outbound (Architecture §3.1, Arc 13).

Inbound: Twilio webhook signature (X-Twilio-Signature) is verified, then the
recipient (the tenant's provisioned number) resolves to the tenant (§3.1.3). The
first outbound SMS of a new conversation carries the AI-identity notice (§3.4.16)
and STOP/opt-out per carrier rules — surfaced via metadata so the runtime prepends it.

The Twilio REST call is abstracted behind an injected sender; real binding in
deployment. The auth token used for signature verification is fetched from
Secrets Manager via the Connections layer (Arc 17) — here it is provided.
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
from app.channels.signatures import verify_twilio


class SmsSender(Protocol):
    async def send_sms(self, from_number: str, to: str, body: str) -> str | None: ...


class SmsAdapter:
    channel_id = "sms"

    def __init__(self, sender: SmsSender) -> None:
        self._sender = sender

    async def verify_inbound(self, request: dict[str, Any]) -> InstanceContext:
        auth_token = request["auth_token"]  # from Secrets Manager (Arc 17)
        url = request["url"]
        params = request["params"]  # the Twilio POST params
        signature = request.get("x_twilio_signature", "")
        if not verify_twilio(auth_token, url, params, signature):
            raise InboundRejected("twilio_signature_invalid")
        resolve = request["resolve"]
        to_number = params.get("To", "")
        ctx: InstanceContext = await resolve("sms_sender", to_number)
        return ctx

    async def receive(self, request: dict[str, Any], ctx: InstanceContext) -> InboundMessage:
        params = request["params"]
        return InboundMessage(
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            channel="sms",
            session_id=None,
            customer_identifier=params.get("From", ""),
            body=params.get("Body", ""),
            channel_metadata={"message_sid": params.get("MessageSid")},
            received_at=datetime.now(UTC),
        )

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        from_number = message.metadata.get("from_number", "")
        body = message.body
        # First message of a new conversation prepends AI-identity + STOP notice (§3.4.16).
        if message.metadata.get("is_first_of_conversation"):
            notice = message.metadata.get(
                "ai_identity_notice", "AI assistant. Reply STOP to opt out."
            )
            body = f"{notice}\n{body}"
        try:
            sid = await self._sender.send_sms(from_number, message.to, body)
        except Exception as exc:  # noqa: BLE001
            return DeliveryReceipt(accepted=False, detail=f"twilio_error:{type(exc).__name__}")
        if sid is None:
            return DeliveryReceipt(accepted=False, detail="twilio_rejected")
        return DeliveryReceipt(accepted=True, provider_message_id=sid)
