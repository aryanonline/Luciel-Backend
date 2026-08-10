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
Meta/email have no reply channel in the webhook response, so the platform pushes Luciel's
answer back out through the channel adapter (``channels.outbound``) before returning the
bare 200 the provider needs as an ack.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
from email import message_from_bytes
from email import policy as email_policy
from email.utils import parseaddr
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request, Response

from app.channels import outbound, pipeline, routing
from app.channels.base import ChannelAdapter, InboundMessage, InboundRejected, InstanceContext
from app.channels.meta_messaging_adapter import MetaMessagingAdapter
from app.channels.runtime_entry import run_inbound_message
from app.channels.sms_adapter import SmsAdapter
from app.channels.sns_verification import ConfigGatedSnsVerifier, confirm_subscription
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


async def _tenant_twilio_auth_token(to_number: str) -> str | None:
    """The receiving tenant's own Twilio Auth Token, or None if they have not connected
    one. Resolving the destination BEFORE verification is safe: it is the same
    SECURITY DEFINER lookup routing performs, reads nothing but the tenant's own stored
    credential, and an unresolvable number simply yields the platform fallback and then
    fails the signature check as before."""
    from app.channels.tenant_twilio import tenant_auth_token

    if not to_number:
        return None
    try:
        ctx = await _resolve("sms_sender", to_number)
    except InboundRejected:
        return None
    async with tenant_session(ctx.admin_id) as s:
        return await tenant_auth_token(s, ctx.admin_id, ctx.instance_id)


async def _run_and_reply(msg: InboundMessage) -> str:
    """Run an admitted InboundMessage through the runtime and deliver Luciel's answer.

    SMS rides back in the TwiML response the caller builds, so it is left alone here.
    Every other channel's provider needs an out-of-band push, so the reply is sent through
    the channel adapter before the webhook acks — otherwise the lead gets silence.
    """
    async with tenant_session(msg.admin_id) as s:
        reply = await run_inbound_message(s, msg)
        if reply.reply_text and msg.channel not in outbound.INLINE_REPLY_CHANNELS:
            await outbound.send_outbound_reply(
                s,
                admin_id=msg.admin_id,
                instance_id=msg.instance_id,
                channel=msg.channel,
                # Reply to the identifier the lead wrote in from (§3.4.9), never a stored
                # contact on another channel.
                to=msg.customer_identifier,
                body=reply.reply_text,
                subject=msg.channel_metadata.get("subject"),
                sender_id=(
                    msg.channel_metadata.get("destination_id")
                    or msg.channel_metadata.get("phone_number_id")
                ),
            )
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
        # Under BYO (§3.1.4) the receiving number belongs to the TENANT's Twilio account,
        # so Twilio signs with THEIR auth token — verifying against the platform's would
        # reject every real inbound. The platform token remains the dev/mock fallback.
        "auth_token": await _tenant_twilio_auth_token(params.get("To", ""))
        or settings.twilio_auth_token,
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
    # SNS subscription confirmation handshake (one-time, on subscribe). Verified with
    # the SAME verifier as a notification before we act on it: confirming an unverified
    # subscription would let anyone bind this endpoint to a topic they control, which is
    # the injection P0-7 exists to close, just one step earlier.
    if sns_message.get("Type") == "SubscriptionConfirmation":
        verifier = ConfigGatedSnsVerifier()
        if not verifier.verify(sns_message):
            return {"status": "rejected", "reason": "sns_signature_invalid"}
        confirmed = get_settings().integration_mode == "live" and confirm_subscription(sns_message)
        return {"status": "subscription_confirmed" if confirmed else "subscription_acknowledged"}
    req: dict[str, Any] = {
        "sns_message": sns_message,
        "resolve": _resolve,
        **_parse_ses_inbound(sns_message),
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
    # compare_digest, not ``==`` (P2-11): the byte-by-byte early exit of a plain compare
    # leaks the token prefix to a caller who can time this endpoint. The ``and token``
    # guard stays — it is what stops an unset secret matching an omitted parameter.
    if mode == "subscribe" and token and hmac.compare_digest(token, settings.meta_verify_token):
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
    # The object also names the split channel id (§3.1.2): an Instagram webhook opens an
    # ``instagram`` session, a Page webhook a ``messenger`` one — never a merged channel.
    obj = payload.get("object", "")
    adapter: ChannelAdapter
    if obj == "whatsapp_business_account":
        adapter = WhatsAppAdapter()
    elif obj == "instagram":
        adapter = MetaMessagingAdapter(channel="instagram")
    else:
        adapter = MetaMessagingAdapter(channel="messenger")
    # Instagram Login is a separate Meta app, so its webhooks are signed with a separate
    # app secret. Falling back to the Facebook one keeps tenants whose IG still rides the
    # Facebook app working while the Instagram app is unprovisioned.
    app_secret = settings.meta_app_secret
    if obj == "instagram" and settings.instagram_app_secret:
        app_secret = settings.instagram_app_secret
    req: dict[str, Any] = {
        "app_secret": app_secret,
        "raw_body": raw,
        "x_hub_signature_256": request.headers.get("X-Hub-Signature-256", ""),
        "payload": payload,
        "resolve": _resolve,
    }
    result = await _dispatch(adapter, req)
    # Meta only needs a 200 ack; Luciel's reply was pushed via the Graph sender in
    # _run_and_reply (the webhook response body cannot carry it).
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

    The verifier does real certificate-pinned RSA verification in live mode (P0-7) and
    accepts in mock/CI so tests stay hermetic. We source the sender from the
    notification layer."""
    from app.channels.email_adapter import EmailAdapter
    from app.notifications.email_sender import build_email_sender

    class _ChannelEmailSender:
        """Adapt the notification EmailSender (send(to, subject, body_text)) to the
        channel EmailSender protocol (send_email(sender, to, subject, body)). Same SES
        transport; the channel passes the tenant's provisioned FROM identity."""

        def __init__(self) -> None:
            self._inner = build_email_sender()

        async def send_email(self, sender: str, to: str, subject: str, body: str) -> str | None:
            return await self._inner.send(to=to, subject=subject, body_text=body)

    return EmailAdapter(sender=_ChannelEmailSender(), sns_verifier=ConfigGatedSnsVerifier())


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _parse_ses_inbound(sns_message: dict[str, Any]) -> dict[str, Any]:
    """Flatten an SES→SNS "Received" notification into the EmailAdapter's request keys.

    The adapter needs the sender, subject and body to normalize an InboundMessage — without
    them the email channel can resolve a tenant but has nothing to answer. The From address
    is reduced to the bare RFC5322 address because that address IS the lead's identity key
    (§3.4.9), and a display name would fragment one lead into many.
    """
    try:
        inner = json.loads(sns_message.get("Message", "{}"))
    except ValueError:
        inner = {}
    mail = inner.get("mail") or {}
    headers = mail.get("commonHeaders") or {}
    receipt = inner.get("receipt") or {}
    destination = mail.get("destination") or []
    from_header = (headers.get("from") or [None])[0] or mail.get("source") or ""
    return {
        "recipient": str(destination[0]) if destination else "",
        "from_address": parseaddr(str(from_header))[1],
        "subject": headers.get("subject"),
        "text": _plain_text_body(inner.get("content")),
        # SES's authentication verdicts (§3.1.5). They inform trust, and NEVER authorize
        # PII disclosure back to the lead (§3.4.9).
        "spf": (receipt.get("spfVerdict") or {}).get("status"),
        "dkim": (receipt.get("dkimVerdict") or {}).get("status"),
        "dmarc": (receipt.get("dmarcVerdict") or {}).get("status"),
    }


def _plain_text_body(content: str | None) -> str:
    """The text/plain part of the base64 MIME message SES inlines in the notification.

    Empty when SES stored the body in S3 instead of inlining it: an empty body is admitted
    and handled downstream rather than 500-ing the provider's webhook.
    """
    if not content:
        return ""
    try:
        raw = base64.b64decode(content, validate=False)
    except (ValueError, binascii.Error):
        return ""
    parsed = message_from_bytes(raw, policy=email_policy.default)
    body = parsed.get_body(preferencelist=("plain",))
    if body is None:
        return ""
    payload = body.get_content()
    return payload if isinstance(payload, str) else ""
