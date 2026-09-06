"""Email channel adapter — SES inbound + outbound (Architecture §3.1.2, Arc 13).

Inbound: SES delivers via SNS; the SNS signature is verified, then the recipient
address resolves to the tenant (§3.1.3). SPF/DKIM/DMARC results from SES are
carried in metadata; a matching From address NEVER authorizes PII disclosure
(§3.4.9). Outbound: sent via SES on the TENANT'S provisioned sender identity — the
per-tenant address stored on the email_sender connection (§3.1.6a / Decision #49),
threaded in via ``OutboundMessage.metadata["sender_identity"]``. The hardcoded
platform address is only a last-resort fallback when no address is configured; the
tenant's configured address always wins.

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
from app.channels.sms_adapter import classify_keyword


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
        # Last-resort fallback ONLY (§3.1.6a): the authoritative sender is the tenant's
        # provisioned address, passed per-message via metadata["sender_identity"]. This
        # default is used only when a tenant has no email address configured.
        self._fallback_identity: str = "luciel@inbound.vantagemind.com"

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
        text = request.get("text", "")
        metadata: dict[str, Any] = {
            "spf": request.get("spf"),
            "dkim": request.get("dkim"),
            "dmarc": request.get("dmarc"),
            "subject": request.get("subject"),
        }
        # A bare STOP/START/HELP as the whole message (or subject) is a consent
        # keyword on email as well (audit F074); a sentence that merely contains the
        # word is not.
        first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
        keyword = classify_keyword(first_line) or classify_keyword(
            str(request.get("subject") or "")
        )
        if keyword is not None:
            metadata["opt_keyword"] = keyword
        return InboundMessage(
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            channel="email",
            session_id=None,
            customer_identifier=request["from_address"],
            body=text,
            channel_metadata=metadata,
            received_at=datetime.now(UTC),
        )

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        # The tenant's provisioned address (§3.1.6a) wins; the platform default is a
        # fallback used only when no per-tenant identity was threaded through.
        sender = message.metadata.get("sender_identity") or self._fallback_identity
        subject = message.metadata.get("subject", "Re: your inquiry")
        try:
            msg_id = await self._sender.send_email(sender, message.to, subject, message.body)
        except Exception as exc:  # noqa: BLE001
            return DeliveryReceipt(accepted=False, detail=f"ses_error:{type(exc).__name__}")
        if msg_id is None:
            return DeliveryReceipt(accepted=False, detail="ses_rejected")
        return DeliveryReceipt(accepted=True, provider_message_id=msg_id)
