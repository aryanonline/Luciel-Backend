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
from app.channels.signatures import verify_twilio_inbound

# Inbound opt-out / help keywords the platform handles AT THE SMS ADAPTER
# (Architecture §3.1.6 / Legal §A2). These are matched case-insensitively against
# the trimmed message body. Twilio Messaging Services also handles STOP/HELP at the
# carrier layer; this app-layer detection is defense-in-depth so the platform-handled
# claim is true even if a webhook reaches us (e.g. Advanced Opt-Out disabled). Matched
# messages are suppressed from the orchestrator/LLM path (audit finding Axis6-F05).
_STOP_KEYWORDS = frozenset({"stop", "stopall", "unsubscribe", "cancel", "end", "quit"})
_HELP_KEYWORDS = frozenset({"help", "info"})
_START_KEYWORDS = frozenset({"start", "unstop", "yes"})


def classify_keyword(body: str) -> str | None:
    """Return 'stop' | 'help' | 'start' if the body is an opt-out/help keyword, else None.

    Carriers treat the message as a keyword only when the whole body (trimmed,
    case-insensitive) is the keyword, so we match the same way.
    """
    word = body.strip().lower()
    if word in _STOP_KEYWORDS:
        return "stop"
    if word in _HELP_KEYWORDS:
        return "help"
    if word in _START_KEYWORDS:
        return "start"
    return None


#: Prepended to the FIRST outbound text of a conversation (§3.4.16 AI identity +
#: carrier opt-out rules): the SMS edge stamps it on a new session's first reply.
SMS_FIRST_MESSAGE_NOTICE = "AI assistant. Reply STOP to opt out."


class SmsSender(Protocol):
    async def send_sms(self, from_number: str, to: str, body: str) -> str | None: ...


class SmsAdapter:
    channel_id = "sms"

    def __init__(self, sender: SmsSender | None = None) -> None:
        # Default wires the real Twilio-backed factory (§3.1.4): live+configured ->
        # TwilioSmsSender, else NullSmsSender (mock/CI, no network). Callers/tests
        # may still inject their own sender (e.g. FakeTwilio) as before — the
        # factory is only consulted when no sender is explicitly supplied.
        if sender is None:
            from app.channels.twilio_sender import build_sms_sender

            sender = build_sms_sender()
        self._sender = sender

    async def verify_inbound(self, request: dict[str, Any]) -> InstanceContext:
        auth_token = request["auth_token"]  # from Secrets Manager (Arc 17)
        url = request["url"]
        params = request["params"]  # the Twilio POST params
        signature = request.get("x_twilio_signature", "")
        # Signature first, byte-identical to before; the capability-URL token
        # (C11) is the ADDITIVE alternate for tenants whose Twilio auth token
        # the platform never holds (OAuth grant / API-key-only submissions).
        if not verify_twilio_inbound(
            auth_token,
            url,
            params,
            signature,
            capability_token=request.get("capability_token"),
        ):
            raise InboundRejected("twilio_signature_invalid")
        resolve = request["resolve"]
        to_number = params.get("To", "")
        ctx: InstanceContext = await resolve("sms_sender", to_number)
        return ctx

    async def receive(self, request: dict[str, Any], ctx: InstanceContext) -> InboundMessage:
        params = request["params"]
        body = params.get("Body", "")
        # STOP/HELP/START detection (§3.1.6). The keyword is surfaced on metadata so
        # the pipeline can suppress routing to the orchestrator/LLM (Axis6-F05).
        keyword = classify_keyword(body)
        metadata: dict[str, Any] = {"message_sid": params.get("MessageSid")}
        if keyword is not None:
            metadata["opt_keyword"] = keyword
        return InboundMessage(
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            channel="sms",
            session_id=None,
            customer_identifier=params.get("From", ""),
            body=body,
            channel_metadata=metadata,
            received_at=datetime.now(UTC),
        )

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        from_number = message.metadata.get("from_number", "")
        body = message.body
        # First message of a new conversation prepends AI-identity + STOP notice (§3.4.16).
        if message.metadata.get("is_first_of_conversation"):
            notice = message.metadata.get("ai_identity_notice", SMS_FIRST_MESSAGE_NOTICE)
            body = f"{notice}\n{body}"
        try:
            sid = await self._sender.send_sms(from_number, message.to, body)
        except Exception as exc:  # noqa: BLE001
            return DeliveryReceipt(accepted=False, detail=f"twilio_error:{type(exc).__name__}")
        if sid is None:
            return DeliveryReceipt(accepted=False, detail="twilio_rejected")
        return DeliveryReceipt(accepted=True, provider_message_id=sid)
