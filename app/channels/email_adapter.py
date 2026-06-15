"""Email channel adapter — SES inbound + outbound (Architecture §3.1.2, Arc 13).

Inbound: SES delivers via SNS; the SNS signature is verified, then the recipient
address resolves to the tenant (§3.1.3). SPF/DKIM/DMARC results from SES are
carried in metadata; a matching From address NEVER authorizes PII disclosure
(§3.4.9). Outbound: sent via SES on the tenant's verified sender identity.

The SES/SNS HTTP/SDK calls are abstracted behind an injected sender so the adapter
is testable; real binding happens in deployment.
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
from app.channels.signatures import sns_canonical_string


class EmailSender(Protocol):
    async def send_email(self, sender: str, to: str, subject: str, body: str) -> str | None: ...


class SnsVerifier(Protocol):
    def verify(self, message: dict[str, Any]) -> bool:
        """Verify the SNS message signature against its SigningCertURL chain."""
        ...


class EmailAdapter:
    channel_id = "email"

    def __init__(self, sender: EmailSender, sns_verifier: SnsVerifier) -> None:
        self._sender = sender
        self._sns = sns_verifier
        self._sender_identity: str = "luciel@inbound.vantagemind.com"

    async def verify_inbound(self, request: dict[str, Any]) -> InstanceContext:
        # request carries the parsed SNS envelope + resolver callback.
        sns_message = request["sns_message"]
        if not self._sns.verify(sns_message):
            raise InboundRejected("sns_signature_invalid")
        # canonical string is recomputable for audit; verification already passed.
        _ = sns_canonical_string(sns_message)
        resolve = request["resolve"]  # async callable: (connection_type, dest) -> InstanceContext
        recipient = request["recipient"]
        ctx: InstanceContext = await resolve("email_sender", recipient)
        return ctx

    async def receive(self, request: dict[str, Any], ctx: InstanceContext) -> InboundMessage:
        return InboundMessage(
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            channel="email",
            session_id=None,
            customer_identifier=request["from_address"],
            body=request.get("text", ""),
            channel_metadata={
                "spf": request.get("spf"),
                "dkim": request.get("dkim"),
                "dmarc": request.get("dmarc"),
                "subject": request.get("subject"),
            },
            received_at=datetime.now(UTC),
        )

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        sender = message.metadata.get("sender_identity", self._sender_identity)
        subject = message.metadata.get("subject", "Re: your inquiry")
        try:
            msg_id = await self._sender.send_email(sender, message.to, subject, message.body)
        except Exception as exc:  # noqa: BLE001
            return DeliveryReceipt(accepted=False, detail=f"ses_error:{type(exc).__name__}")
        if msg_id is None:
            return DeliveryReceipt(accepted=False, detail="ses_rejected")
        return DeliveryReceipt(accepted=True, provider_message_id=msg_id)
