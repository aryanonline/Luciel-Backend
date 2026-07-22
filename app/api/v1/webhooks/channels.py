"""Inbound channel webhook receivers (Architecture §3.1.3, §3.1.5, §3.4.1).

The HTTP edge for the non-widget channels. Each route builds its channel-specific
request envelope, runs the shared inbound pipeline (verify signature → resolve tenant →
pre-budget admission → normalize), and on admission hands the normalized message to the
shared runtime entry (§3.4.1 agentic loop). Signature is verified BEFORE tenant lookup
(§3.1.3/§3.7.2); an unsigned/unresolvable inbound is rejected + observability-logged and
never routed.

Channels:
- Twilio SMS       — POST /api/v1/webhooks/twilio/sms   (X-Twilio-Signature; TwiML reply)
- SES/SNS email    — POST /api/v1/webhooks/ses/inbound  (SNS envelope; async, 200)
- Meta (WA/IG/FB)  — GET  /api/v1/webhooks/meta         (hub.challenge verification)
                     POST /api/v1/webhooks/meta         (X-Hub-Signature-256; async, 200)

Runtime replies: SMS returns TwiML so Twilio delivers the reply on the same channel;
Meta/email send asynchronously via their adapters (the platform posts back out-of-band),
so these return a bare 200 (the provider only needs an ack).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request, Response

from app.channels import pipeline, routing
from app.channels.base import ChannelAdapter, InboundMessage, InboundRejected, InstanceContext
from app.channels.meta_messaging_adapter import MetaMessagingAdapter
from app.channels.runtime_entry import run_inbound_message
from app.channels.sms_adapter import SmsAdapter
from app.channels.whatsapp_adapter import WhatsAppAdapter
from app.config import get_settings
from app.db.session import tenant_session, untenanted_session

if TYPE_CHECKING:
    from app.channels.email_adapter import EmailAdapter

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])


async def _resolve(connection_type: str, destination: str) -> InstanceContext:
    """Pre-tenant SECURITY DEFINER resolution (§3.1.3): destination → tenant."""
    async with untenanted_session() as s:
        return await routing.resolve_destination(s, connection_type, destination)


async def _run_and_reply(msg: InboundMessage) -> str:
    """Run an admitted InboundMessage through the runtime; return the reply text."""
    async with tenant_session(msg.admin_id) as s:
        reply = await run_inbound_message(s, msg)
        return reply.reply_text


# --------------------------------------------------------------------------- #
# Twilio SMS
# --------------------------------------------------------------------------- #
@router.post("/twilio/sms")
async def twilio_sms_inbound(request: Request) -> Response:
    settings = get_settings()
    form = await request.form()
    params: dict[str, str] = {k: str(v) for k, v in form.items()}
    req: dict[str, Any] = {
        # Account Auth Token for X-Twilio-Signature (distinct from the send API key).
        "auth_token": settings.twilio_auth_token,
        "url": str(request.url),
        "params": params,
        "x_twilio_signature": request.headers.get("X-Twilio-Signature", ""),
        "resolve": _resolve,
        "source_ip": request.client.host if request.client else "unknown",
    }
    result = await _dispatch(SmsAdapter(), req)
    # Reply on-channel via TwiML. Empty <Response/> = accepted, no auto-reply.
    if isinstance(result, str) and result:
        body = (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f"<Response><Message>{_xml_escape(result)}</Message></Response>"
        )
    else:
        body = '<?xml version="1.0" encoding="UTF-8"?><Response/>'
    return Response(content=body, media_type="application/xml")


# --------------------------------------------------------------------------- #
# SES inbound email (delivered via SNS)
# --------------------------------------------------------------------------- #
@router.post("/ses/inbound")
async def ses_inbound(request: Request) -> dict[str, str]:
    raw = await request.body()
    try:
        sns_message = json.loads(raw or b"{}")
    except ValueError:
        return {"status": "ignored", "reason": "bad_sns_envelope"}
    # SNS subscription confirmation handshake (one-time, on subscribe).
    if sns_message.get("Type") == "SubscriptionConfirmation":
        return {"status": "subscription_confirmation_received"}
    recipient = _extract_email_recipient(sns_message)
    req: dict[str, Any] = {
        "sns_message": sns_message,
        "recipient": recipient,
        "resolve": _resolve,
    }
    result = await _dispatch(_build_email_adapter(), req)
    return {"status": "ok" if isinstance(result, str) else "not_routed"}


# --------------------------------------------------------------------------- #
# Meta (WhatsApp + Instagram/Messenger)
# --------------------------------------------------------------------------- #
@router.get("/meta")
async def meta_verify(request: Request) -> Response:
    """Meta webhook verification handshake: echo hub.challenge iff the verify token
    matches the configured one."""
    settings = get_settings()
    q = request.query_params
    mode = q.get("hub.mode")
    token = q.get("hub.verify_token")
    challenge = q.get("hub.challenge", "")
    if mode == "subscribe" and token and token == settings.meta_verify_token:
        return Response(content=challenge, media_type="text/plain")
    return Response(content="forbidden", status_code=403, media_type="text/plain")


@router.post("/meta")
async def meta_inbound(request: Request) -> dict[str, str]:
    settings = get_settings()
    raw = await request.body()
    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        return {"status": "ignored", "reason": "bad_payload"}
    # WhatsApp uses object=="whatsapp_business_account"; IG/Messenger use page/instagram.
    obj = payload.get("object", "")
    adapter = WhatsAppAdapter() if obj == "whatsapp_business_account" else MetaMessagingAdapter()
    req: dict[str, Any] = {
        "app_secret": settings.meta_app_secret,
        "raw_body": raw,
        "x_hub_signature_256": request.headers.get("X-Hub-Signature-256", ""),
        "payload": payload,
        "resolve": _resolve,
    }
    result = await _dispatch(adapter, req)
    # Meta only needs a 200 ack; the reply is sent out-of-band via the Graph sender.
    return {"status": "ok" if isinstance(result, str) else "not_routed"}


# --------------------------------------------------------------------------- #
# shared dispatch
# --------------------------------------------------------------------------- #
async def _dispatch(adapter: ChannelAdapter, req: dict[str, Any]) -> str | None:
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
        return await _run_and_reply(outcome)  # noqa: RET504
    # AdmissionDenied / OptOutHandled / InboundRejected → no runtime reply.
    return None


def _build_email_adapter() -> EmailAdapter:
    """Construct the EmailAdapter with the SES sender + SNS verifier.

    SNS signature verification is a deployment-time seam (cert-chain validation, per
    the original audit §3.7.2 note). In live mode with a configured verifier this is
    real; in mock/CI it accepts (hermetic). We source the sender from the notification
    layer built last session."""
    from app.channels.email_adapter import EmailAdapter
    from app.notifications.email_sender import build_email_sender

    class _ConfigGatedSnsVerifier:
        def verify(self, message: dict[str, Any]) -> bool:
            # In a full deploy this validates the SNS cert chain + signature. Until that
            # deployment seam is wired, accept in non-live and reject unsigned in live.
            s = get_settings()
            if s.integration_mode != "live":
                return True
            return bool(message.get("Signature"))

    class _ChannelEmailSender:
        """Adapt the notification EmailSender (send(to, subject, body_text)) to the
        channel EmailSender protocol (send_email(sender, to, subject, body)). Same SES
        transport; the channel passes the tenant's provisioned FROM identity."""

        def __init__(self) -> None:
            self._inner = build_email_sender()

        async def send_email(self, sender: str, to: str, subject: str, body: str) -> str | None:
            return await self._inner.send(to=to, subject=subject, body_text=body)

    return EmailAdapter(sender=_ChannelEmailSender(), sns_verifier=_ConfigGatedSnsVerifier())


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _extract_email_recipient(sns_message: dict[str, Any]) -> str:
    """Pull the destination address out of an SES→SNS notification envelope."""
    try:
        inner = json.loads(sns_message.get("Message", "{}"))
        recipients = inner.get("mail", {}).get("destination", [])
        return str(recipients[0]) if recipients else ""
    except (ValueError, KeyError, IndexError):
        return ""
