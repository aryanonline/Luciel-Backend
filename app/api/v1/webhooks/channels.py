"""Inbound channel webhook receivers (Architecture §3.1.3, §3.1.5, §3.4.1).

The HTTP edge for the non-widget channels. Each route builds its channel-specific
request envelope, runs the shared inbound pipeline (verify signature → resolve tenant →
pre-budget admission → normalize), and on admission hands the normalized message to the
shared runtime entry (§3.4.1 agentic loop). Signature is verified BEFORE tenant lookup
(§3.1.3/§3.7.2); an unsigned/unresolvable inbound is rejected + observability-logged and
never routed.

Channels:
- Twilio SMS       — POST /api/v1/webhooks/twilio/sms   (X-Twilio-Signature; TwiML reply)
- Twilio Voice     — POST /api/v1/webhooks/twilio/voice       (call start; TwiML)
                     POST /api/v1/webhooks/twilio/voice/turn  (Gather action; TwiML)
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

from app.channels import inbound_dispatch, routing
from app.channels.base import ChannelAdapter, InboundRejected, InstanceContext
from app.channels.meta_messaging_adapter import MetaMessagingAdapter
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


# Shared with the mailbox poll sweep (pull ingress) — see channels/inbound_dispatch.
# Module-level aliases keep this module's public shape (and its tests) stable.
_run_and_reply = inbound_dispatch.run_and_reply


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
# Twilio Voice (Arc 14b — the ingress that finally makes the built pipeline
# reachable: adapter, disclosure, TwiML, and call records all existed with no
# webhook route, so an enabled Voice channel answered nothing)
# --------------------------------------------------------------------------- #


class TwilioGatherTranscript:
    """Honest 'STT' for the ``<Gather input="speech">`` flow: Twilio's own speech
    recognition already transcribed the caller — ``SpeechResult`` IS the
    transcript — so this passthrough names the real recognizer rather than
    re-transcribing text. Whisper binds on a recording-based flow, which this
    Gather loop does not produce."""

    name = "twilio_gather_stt"

    async def transcribe(self, audio_ref: str) -> Any:
        from app.runtime.speech import Transcription  # noqa: PLC0415

        return Transcription(text=audio_ref)


class TwilioSayDelivery:
    """Honest 'TTS' for TwiML delivery: the spoken reply is rendered by Twilio's
    ``<Say>`` in the webhook response — no synthesis API is called, because
    paying a TTS provider to produce audio the response then discards would be
    waste, not delivery. The ``<Play>`` audio-hosting surface now exists
    (app/runtime/voice_audio.py): in live mode with a provisioned bucket the
    route binds ``HostedAudioTts`` instead, and this class remains the mock
    posture AND the fallback whenever hosting cannot serve."""

    name = "twilio_say_tts"

    async def synthesize(self, text: str) -> Any:
        from app.runtime.speech import SynthesizedSpeech  # noqa: PLC0415

        return SynthesizedSpeech(audio_ref=f"twiml-say:{len(text)}", text=text)


def _voice_enabled(inst: Any) -> bool:
    channels = inst.channels or []
    return any(
        isinstance(c, dict) and c.get("id") == "voice" and bool(c.get("enabled")) for c in channels
    )


def _voice_unavailable_twiml() -> Response:
    """A closed, honest reply for every not-handled case (disabled channel,
    paused, rejected inbound): a spoken notice + hangup. Twilio must always
    receive valid TwiML — an empty 200 leaves dead air on a live call."""
    from app.channels.voice_adapter import SAY_VOICE  # noqa: PLC0415
    from app.runtime.voice_pipeline import VOICE_UNAVAILABLE_REPLY  # noqa: PLC0415

    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<Response><Say voice="{SAY_VOICE}">{_xml_escape(VOICE_UNAVAILABLE_REPLY)}</Say>'
        "<Hangup/></Response>"
    )
    return Response(content=body, media_type="application/xml")


def _voice_turn_action_url(request: Request) -> str:
    """Absolute URL Twilio posts the caller's next utterance to. Derived from the
    request URL (ALB-terminated HTTPS — --proxy-headers is load-bearing here,
    exactly as for signature verification)."""
    return str(request.url.replace(path="/api/v1/webhooks/twilio/voice/turn", query=""))


async def _voice_request(request: Request) -> dict[str, Any]:
    settings = get_settings()
    form = await request.form()
    params: dict[str, str] = {k: str(v) for k, v in form.items()}
    return {
        # BYO (§3.1.4): the called number belongs to the TENANT's Twilio account,
        # so Twilio signs with THEIR auth token — same resolution as SMS.
        "auth_token": await _tenant_twilio_auth_token(params.get("To", ""))
        or settings.twilio_auth_token,
        "url": str(request.url),
        "params": params,
        "x_twilio_signature": request.headers.get("X-Twilio-Signature", ""),
        "resolve": _resolve,
        "source_ip": request.client.host if request.client else "unknown",
    }


@router.post("/twilio/voice")
async def twilio_voice_inbound(request: Request) -> Response:
    """Call start: verify → resolve → admit, then play the mandatory disclosure
    and gather the caller's first utterance (§3.1.2 — disclosure BEFORE any
    caller audio is processed)."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.channels import pipeline  # noqa: PLC0415
    from app.channels.base import InboundMessage, InboundRejected  # noqa: PLC0415
    from app.channels.voice_adapter import VoiceAdapter  # noqa: PLC0415
    from app.models.instance import Instance, is_paused  # noqa: PLC0415
    from app.runtime import voice_pipeline  # noqa: PLC0415

    req = await _voice_request(request)
    async with untenanted_session() as s:
        try:
            outcome = await pipeline.ingest(VoiceAdapter(), req, s)
        except InboundRejected as rej:
            await pipeline.record_inbound_rejection(s, reason=rej.reason, channel="voice")
            return _voice_unavailable_twiml()
    if not isinstance(outcome, InboundMessage):
        # AdmissionDenied (rate/abuse) or opt-out keyword — no call handling.
        return _voice_unavailable_twiml()

    async with tenant_session(outcome.admin_id) as s:
        inst = (
            await s.execute(select(Instance).where(Instance.id == outcome.instance_id))
        ).scalar_one_or_none()
        # Paused → no handling and NO disclosure record (§3.6.2); a disabled
        # channel likewise never claims to answer (§3.8.7 rule A).
        if inst is None or is_paused(inst) or not _voice_enabled(inst):
            return _voice_unavailable_twiml()
        twiml = await voice_pipeline.start_call(
            s,
            instance=inst,
            admin_id=outcome.admin_id,
            instance_id=outcome.instance_id,
            provider_call_id=str(outcome.channel_metadata.get("call_sid") or ""),
            from_number=outcome.customer_identifier,
            action_url=_voice_turn_action_url(request),
        )
    return Response(content=twiml, media_type="application/xml")


@router.post("/twilio/voice/turn")
async def twilio_voice_turn(request: Request) -> Response:
    """One caller utterance (Twilio ``<Gather>`` action) → spoken reply, through
    the SAME orchestrator + budget gate as every other channel (§3.4.1b)."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.billing import metering  # noqa: PLC0415
    from app.channels import pipeline  # noqa: PLC0415
    from app.channels.base import InboundMessage, InboundRejected  # noqa: PLC0415
    from app.channels.voice_adapter import VoiceAdapter, reply_twiml  # noqa: PLC0415
    from app.models.instance import Instance  # noqa: PLC0415
    from app.runtime import voice_pipeline  # noqa: PLC0415
    from app.runtime.provider_factory import build_llm_router  # noqa: PLC0415
    from app.runtime.voice_audio import build_hosted_voice_tts  # noqa: PLC0415

    req = await _voice_request(request)
    async with untenanted_session() as s:
        try:
            outcome = await pipeline.ingest(VoiceAdapter(), req, s)
        except InboundRejected as rej:
            await pipeline.record_inbound_rejection(s, reason=rej.reason, channel="voice")
            return _voice_unavailable_twiml()
    if not isinstance(outcome, InboundMessage):
        return _voice_unavailable_twiml()

    action_url = _voice_turn_action_url(request)
    if not outcome.body.strip():
        # Gather timed out with no speech: reprompt without invoking the runtime
        # (silence must not consume the tenant's conversation budget).
        body = reply_twiml("Sorry, I didn't catch that. Could you say that again?", action_url)
        return Response(content=body, media_type="application/xml")

    async with tenant_session(outcome.admin_id) as s:
        inst = (
            await s.execute(select(Instance).where(Instance.id == outcome.instance_id))
        ).scalar_one_or_none()
        if inst is None or not _voice_enabled(inst):
            return _voice_unavailable_twiml()
        # handle_turn owns the paused branch (spoken unavailable + hangup, the
        # shared is_paused gate) and the at-cap graceful no-LLM reply.
        call_sid = str(outcome.channel_metadata.get("call_sid") or "")
        result = await voice_pipeline.handle_turn(
            s,
            instance=inst,
            admin_id=outcome.admin_id,
            instance_id=outcome.instance_id,
            from_number=outcome.customer_identifier,
            speech_audio_ref=outcome.body,
            action_url=action_url,
            stt=TwilioGatherTranscript(),
            # Hosted synthesis (<Play> via ElevenLabs + S3, §3.1.2) when live mode
            # AND the bucket exist; the honest <Say> delivery otherwise — and the
            # hosted wrapper itself degrades to <Say> on any failure, so a worse
            # voice never becomes a dead call.
            tts=build_hosted_voice_tts(
                admin_id=outcome.admin_id, instance_id=outcome.instance_id, call_sid=call_sid
            )
            or TwilioSayDelivery(),
            router=build_llm_router(admin_id=outcome.admin_id),
            has_payment_method=await metering.has_live_card(s, admin_id=outcome.admin_id),
            provider_call_id=call_sid or None,
        )
    return Response(content=result.twiml, media_type="application/xml")


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
# shared dispatch — see channels/inbound_dispatch (shared with the mailbox poll)
# --------------------------------------------------------------------------- #
_dispatch = inbound_dispatch.dispatch_inbound


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
