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

import asyncio
import base64
import binascii
import hmac
import json
import logging
import uuid
from email import message_from_bytes
from email import policy as email_policy
from email.utils import parseaddr
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request, Response

from app.channels import inbound_dispatch, routing
from app.channels.base import ChannelAdapter, InboundRejected, InstanceContext
from app.channels.meta_messaging_adapter import MetaMessagingAdapter
from app.channels.sms_adapter import SMS_FIRST_MESSAGE_NOTICE, SmsAdapter
from app.channels.sns_verification import ConfigGatedSnsVerifier, confirm_subscription
from app.channels.whatsapp_adapter import WhatsAppAdapter
from app.config import get_settings
from app.db.session import tenant_session, untenanted_session

if TYPE_CHECKING:
    from app.channels.email_adapter import EmailAdapter

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)


async def _resolve(connection_type: str, destination: str) -> InstanceContext:
    """Pre-tenant SECURITY DEFINER resolution (§3.1.3): destination → tenant."""
    async with untenanted_session() as s:
        return await routing.resolve_destination(s, connection_type, destination)


async def _tenant_twilio_inbound_auth(to_number: str) -> tuple[str | None, str | None]:
    """The receiving tenant's inbound-authenticity secrets: (auth_token, capability).

    Resolving the destination BEFORE verification is safe: it is the same
    SECURITY DEFINER lookup routing performs, reads nothing but the tenant's own
    stored credential, and an unresolvable number simply yields the platform
    fallback and then fails the signature check as before. The capability token
    (C11) covers tenants whose Account Auth Token the platform never holds —
    the one-click OAuth grant and API-key-only submissions — whose registered
    webhook URLs carry the matching ``cap`` parameter instead."""
    from app.channels.tenant_twilio import tenant_inbound_verification

    if not to_number:
        return None, None
    try:
        ctx = await _resolve("sms_sender", to_number)
    except InboundRejected:
        return None, None
    async with tenant_session(ctx.admin_id) as s:
        return await tenant_inbound_verification(s, ctx.admin_id, ctx.instance_id)


async def _audit_capability_rejection(to_number: str) -> None:
    """Audit a forged/mismatched capability-URL inbound under its tenant (C11).

    Content-route rejections stay pre-tenant by design (Axis6-F06 — no tenant
    scope before verification), but a bad ``cap`` on a RESOLVABLE number names
    its tenant, and the owner deserves to see forged traffic against their
    number in their audit log. Best-effort: an unresolvable number simply logs."""
    from app.audit import log as audit_log  # noqa: PLC0415
    from app.models.audit import ActorType  # noqa: PLC0415

    if not to_number:
        return
    try:
        ctx = await _resolve("sms_sender", to_number)
    except InboundRejected:
        return
    async with tenant_session(ctx.admin_id) as s:
        await audit_log.emit(
            s,
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            actor_type=ActorType.platform,
            event_type="inbound_capability_rejected",
            payload={"connection_type": "sms_sender", "number_suffix": to_number[-4:]},
        )


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
    # Account Auth Token for X-Twilio-Signature (distinct from the send API key).
    # Under BYO (§3.1.4) the receiving number belongs to the TENANT's Twilio account,
    # so Twilio signs with THEIR auth token — verifying against the platform's would
    # reject every real inbound. The platform token remains the dev/mock fallback.
    # The capability token (C11) is the alternate proof for tenants with no stored
    # auth token; None for everyone else.
    tenant_token, capability = await _tenant_twilio_inbound_auth(params.get("To", ""))
    req: dict[str, Any] = {
        "auth_token": tenant_token or settings.twilio_auth_token,
        "capability_token": capability,
        "url": str(request.url),
        "params": params,
        "x_twilio_signature": request.headers.get("X-Twilio-Signature", ""),
        "resolve": _resolve,
        "source_ip": request.client.host if request.client else "unknown",
    }
    reply = await inbound_dispatch.dispatch_inbound_reply(SmsAdapter(), req)
    # Reply on-channel via TwiML. Empty <Response/> = accepted, no auto-reply.
    text = reply.reply_text if reply is not None else ""
    if text and reply is not None and reply.is_new_session:
        # The FIRST text of a conversation carries the AI identity + STOP notice
        # (§3.4.16 / carrier rules). Audit F064: the adapter waited for a metadata
        # flag nothing ever set, so no conversation ever got it.
        text = f"{SMS_FIRST_MESSAGE_NOTICE}\n{text}"
    if text:
        body = (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f"<Response><Message>{_xml_escape(text)}</Message></Response>"
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
    # BYO (§3.1.4): the called number belongs to the TENANT's Twilio account,
    # so Twilio signs with THEIR auth token — same resolution as SMS, and the
    # same C11 capability-token alternate for tenants without a stored token.
    tenant_token, capability = await _tenant_twilio_inbound_auth(params.get("To", ""))
    return {
        "auth_token": tenant_token or settings.twilio_auth_token,
        "capability_token": capability,
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
        try:
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
        except InboundRejected as rej:
            # A turn with neither a CallSid nor a From has nothing to key its session
            # on (sessions.SessionUnkeyable, round 7 WP-3a): recorded like any other
            # rejected inbound, answered with the closed TwiML — never a 5xx.
            await pipeline.record_inbound_rejection(s, reason=rej.reason, channel="voice")
            return _voice_unavailable_twiml()
    return Response(content=result.twiml, media_type="application/xml")


@router.post("/twilio/voice/status")
async def twilio_voice_status(request: Request) -> Response:
    """Call-status callback: finalize the call's session at the REAL hangup (§3.4.8
    trigger (b) — the voice twin of the widget close beacon).

    Twilio posts here when the call reaches a terminal state (auto-configured on the
    designated number alongside the voice/SMS webhooks). Finalizing at hangup writes
    the summary immediately and stamps ``ended_at`` with the call's actual end — so
    the next call from the same caller is a NEW conversation and the dashboard row
    stops reading as in-progress. The hourly inactivity sweep stays as the backstop
    for calls whose callback never arrives. NOT the inbound pipeline: a status
    callback carries no conversational content, so it takes signature → resolve →
    finalize, with no admission/opt-out semantics.
    """
    from sqlalchemy import select  # noqa: PLC0415

    from app.channels import pipeline, rate_limit  # noqa: PLC0415
    from app.channels.signatures import verify_twilio_inbound  # noqa: PLC0415
    from app.models.voice import VoiceCallRecord  # noqa: PLC0415
    from app.runtime.session_close import finalize_session  # noqa: PLC0415

    req = await _voice_request(request)
    params: dict[str, str] = req["params"]
    # C11: this route bypasses pipeline.ingest (no conversational content), so it
    # previously had NO admission control at all. Per-destination-number bucket —
    # legit volume is one callback per call plus retries.
    admitted = rate_limit.get_store().check_source("voice_status", params.get("To", "") or "?")
    if not admitted.allowed:
        return Response(status_code=429)
    async with untenanted_session() as s:
        if not verify_twilio_inbound(
            req["auth_token"],
            req["url"],
            params,
            req["x_twilio_signature"],
            capability_token=req.get("capability_token"),
        ):
            await pipeline.record_inbound_rejection(
                s, reason="twilio_signature_invalid", channel="voice"
            )
            # A cap parameter that was PRESENTED but did not verify is a forgery
            # signal on a tenant we can name (the To number resolved when the
            # capability token was fetched) — audit it under that tenant (C11).
            if "cap=" in str(request.url.query or ""):
                await _audit_capability_rejection(params.get("To", ""))
            return Response(status_code=403)
        try:
            ctx = await _resolve("sms_sender", params.get("To", ""))
        except InboundRejected as rej:
            await pipeline.record_inbound_rejection(s, reason=rej.reason, channel="voice")
            return Response(status_code=200)

    call_sid = params.get("CallSid", "")
    call_status = params.get("CallStatus", "")
    if not call_sid or call_status not in ("completed", "busy", "failed", "no-answer", "canceled"):
        return Response(status_code=200)

    async with tenant_session(ctx.admin_id) as s:
        record = (
            (
                await s.execute(
                    select(VoiceCallRecord).where(
                        VoiceCallRecord.admin_id == ctx.admin_id,
                        VoiceCallRecord.instance_id == ctx.instance_id,
                        VoiceCallRecord.provider_call_id == call_sid,
                        VoiceCallRecord.session_id.is_not(None),
                    )
                )
            )
            .scalars()
            .first()
        )
        if record is not None and record.session_id is not None:
            # Idempotent: an already-ended session is a no-op inside finalize_session.
            await finalize_session(
                s,
                admin_id=ctx.admin_id,
                instance_id=ctx.instance_id,
                channel="voice",
                session_id=record.session_id,
            )
    return Response(status_code=200)


#: Seconds to wait before looking an unknown receipt SID up again (round 7 delivery
#: hardening): the sending transaction commits within moments of the API accept.
RECEIPT_LOOKUP_RETRY_DELAYS: tuple[float, ...] = (0.5, 1.5, 3.0)


@router.post("/twilio/notifications/status")
async def twilio_notification_status(request: Request) -> Response:
    """Delivery receipt for an OWNER notification text (round 7 WP-4b).

    The escalation SMS leg asks Twilio to post each page's ``MessageStatus`` here, so
    ``escalation_events.delivery_status`` reads ``delivered`` when the carrier delivered
    it and ``failed`` (with the owner paged by email) when it did not — Twilio accepting
    the request used to be the whole proof. Same posture as the voice status route:
    verify → resolve → stamp, no admission/opt-out semantics, and the tenant is the
    owner of the FROM number (their own BYO number, the one the page went out on),
    never the To. A receipt for a message this platform did not send is a no-op 200.

    Round 7 delivery hardening: the signature (or capability token) is checked BEFORE
    the per-tenant flood bucket is charged, so unsigned traffic can never blind a
    tenant's real receipts; and a verified receipt whose SID no row carries yet (it
    raced the sending transaction's commit) is looked up again for a few seconds
    before it is let go — Twilio does not retry a status callback.
    """
    from app.channels import pipeline, rate_limit  # noqa: PLC0415
    from app.channels.signatures import verify_twilio_inbound  # noqa: PLC0415
    from app.notifications import escalation_dispatch  # noqa: PLC0415

    settings = get_settings()
    form = await request.form()
    params: dict[str, str] = {k: str(v) for k, v in form.items()}
    from_number = params.get("From", "")
    tenant_token, capability = await _tenant_twilio_inbound_auth(from_number)
    async with untenanted_session() as s:
        if not verify_twilio_inbound(
            tenant_token or settings.twilio_auth_token,
            str(request.url),
            params,
            request.headers.get("X-Twilio-Signature", ""),
            capability_token=capability,
        ):
            await pipeline.record_inbound_rejection(
                s, reason="twilio_signature_invalid", channel="sms"
            )
            if "cap=" in str(request.url.query or ""):
                await _audit_capability_rejection(from_number)
            return Response(status_code=403)
        # Flood control AFTER verification (round 7 delivery hardening): the bucket
        # used to be charged on the unauthenticated From field, so anyone who knew a
        # business's number could exhaust it with unsigned posts, auto-block the key
        # for a day, and blind every real receipt for that tenant (429) — a carrier
        # failure then never reached the owner. Only a receipt Twilio signed (or that
        # carries the tenant's capability token) spends from the tenant's bucket now;
        # a forgery costs the forger a 403 and nothing of the tenant's.
        admitted = rate_limit.get_store().check_source(
            "voice_status", f"notification:{from_number or '?'}"
        )
        if not admitted.allowed:
            return Response(status_code=429)
        try:
            ctx = await _resolve("sms_sender", from_number)
        except InboundRejected as rej:
            await pipeline.record_inbound_rejection(s, reason=rej.reason, channel="sms")
            return Response(status_code=200)

    message_sid = params.get("MessageSid") or params.get("SmsSid") or ""
    message_status = params.get("MessageStatus") or params.get("SmsStatus") or ""
    if not message_sid or not message_status:
        return Response(status_code=200)
    # A receipt can arrive BEFORE the sending transaction commits (Twilio posts within
    # seconds; the deferred sweep sends all of a tenant's queued pages in one
    # transaction), when no row carries the SID yet. Twilio does not retry a status
    # callback, so an unknown SID is looked up again after short waits — each in a
    # fresh transaction, so a commit in between is seen — before it is let go; the
    # sweep's status fetch (``sweep_stale_escalations``) is the backstop past that.
    # Only a VERIFIED receipt reaches this wait: nothing unsigned can make it sleep.
    for delay in (0.0, *RECEIPT_LOOKUP_RETRY_DELAYS):
        if delay:
            await asyncio.sleep(delay)
        async with tenant_session(ctx.admin_id) as s:
            outcome = await escalation_dispatch.record_sms_delivery_receipt(
                s,
                admin_id=ctx.admin_id,
                instance_id=ctx.instance_id,
                provider_message_id=message_sid,
                message_status=message_status,
                error_code=params.get("ErrorCode") or None,
            )
        if outcome != "unknown_message":
            break
    return Response(status_code=200)


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
    settings = get_settings()
    live = settings.integration_mode == "live"
    # A signed message from SOMEONE ELSE'S topic is still a valid SNS signature
    # (2026-09-05 audit F048): pin the receipt-rule topic the way the events route
    # does, so a stranger cannot feed this tenant inbound mail through their own topic.
    # Fail CLOSED in live mode (round 7 WP-7): the pin used to be skipped whenever the
    # ARN was unset — and ComputeStack never injected it, so every live deploy ran
    # unpinned. No pin, no inbound mail.
    pinned_topics = settings.ses_inbound_topic_arns
    if live and not pinned_topics:
        logger.error("ses_inbound_rejected reason=topic_unpinned (SES_INBOUND_TOPIC_ARN unset)")
        return {"status": "rejected", "reason": "topic_unpinned"}
    if live and sns_message.get("TopicArn") not in pinned_topics:
        return {"status": "rejected", "reason": "topic_mismatch"}
    # SNS subscription confirmation handshake (one-time, on subscribe). Verified with
    # the SAME verifier as a notification before we act on it: confirming an unverified
    # subscription would let anyone bind this endpoint to a topic they control, which is
    # the injection P0-7 exists to close, just one step earlier.
    if sns_message.get("Type") == "SubscriptionConfirmation":
        verifier = ConfigGatedSnsVerifier()
        if not verifier.verify(sns_message):
            return {"status": "rejected", "reason": "sns_signature_invalid"}
        confirmed = live and confirm_subscription(sns_message)
        return {"status": "subscription_confirmed" if confirmed else "subscription_acknowledged"}
    req: dict[str, Any] = {
        "sns_message": sns_message,
        "resolve": _resolve,
        **_parse_ses_inbound(sns_message),
    }
    result = await _dispatch(_build_email_adapter(), req)
    return {"status": "ok" if isinstance(result, str) else "not_routed"}


# --------------------------------------------------------------------------- #
# SES delivery events — bounce/complaint (delivered via SNS, round 5B item 13)
# --------------------------------------------------------------------------- #
@router.post("/ses/events")
async def ses_events(request: Request) -> dict[str, str]:
    """Record SES bounce/complaint events against the tenant's contact addresses.

    SES accepting a message proves nothing — a bouncing escalation contact used
    to swallow hot-lead notifications with a green audit trail. The event's
    ``mail.tags`` carry ``vm_admin_id`` (stamped on every platform send), so
    attribution needs no address scan; an untagged event is unattributable and
    ignored. TOPIC PINNING on top of signature verification: any AWS account can
    subscribe this public URL to THEIR topic and send genuinely-signed
    notifications — with a fabricated vm_admin_id that would let an attacker
    suppress a victim's escalation contact (a DoS on hot-lead delivery). In live
    mode only the configured events topic is accepted."""
    raw = await request.body()
    try:
        sns_message = json.loads(raw or b"{}")
    except ValueError:
        return {"status": "ignored", "reason": "bad_sns_envelope"}
    settings = get_settings()
    live = settings.integration_mode == "live"
    pinned_topic = settings.ses_events_topic_arn
    if live and pinned_topic and sns_message.get("TopicArn") != pinned_topic:
        return {"status": "rejected", "reason": "topic_mismatch"}
    if sns_message.get("Type") == "SubscriptionConfirmation":
        verifier = ConfigGatedSnsVerifier()
        if not verifier.verify(sns_message):
            return {"status": "rejected", "reason": "sns_signature_invalid"}
        confirmed = live and confirm_subscription(sns_message)
        return {"status": "subscription_confirmed" if confirmed else "subscription_acknowledged"}
    verifier = ConfigGatedSnsVerifier()
    if not verifier.verify(sns_message):
        return {"status": "rejected", "reason": "sns_signature_invalid"}
    try:
        event = json.loads(sns_message.get("Message") or "{}")
    except ValueError:
        return {"status": "ignored", "reason": "bad_event_payload"}
    event_type = event.get("eventType") or event.get("notificationType") or ""
    if event_type not in ("Bounce", "Complaint"):
        return {"status": "ignored", "reason": "not_a_delivery_incident"}
    tags = (event.get("mail") or {}).get("tags") or {}
    admin_raw = tags.get("vm_admin_id") or []
    admin_value = admin_raw[0] if isinstance(admin_raw, list) and admin_raw else admin_raw
    try:
        admin_id = uuid.UUID(str(admin_value))
    except (ValueError, TypeError):
        return {"status": "ignored", "reason": "unattributable"}
    purpose_raw = tags.get("vm_purpose") or []
    purpose = purpose_raw[0] if isinstance(purpose_raw, list) and purpose_raw else str(purpose_raw)

    from app.notifications.delivery_incidents import record_ses_event  # noqa: PLC0415

    recorded = await record_ses_event(
        admin_id=admin_id, event_type=event_type, event=event, purpose=str(purpose or "")
    )
    return {"status": "ok" if recorded else "ignored"}


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

        async def send_email(
            self,
            sender: str,
            to: str,
            subject: str,
            body: str,
            *,
            headers: dict[str, str] | None = None,
        ) -> str | None:
            # This adapter is the INBOUND half of the SES channel; every reply leaves
            # through ``channels.outbound`` (which threads ``headers``). Accepted so
            # the adapter keeps satisfying the EmailSender protocol; never reached.
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
        # Case-folded (round 7 WP-3c item 10): the routing claim is stored lower-case.
        "recipient": str(destination[0]).strip().lower() if destination else "",
        "from_address": parseaddr(str(from_header))[1],
        "subject": headers.get("subject"),
        # The customer's RFC 5322 Message-ID (``mail.commonHeaders.messageId``): the
        # session keeps it so a later takeover reply threads under their mail with
        # In-Reply-To / References (round 7 WP-3b, item 2). None when SES omitted it.
        "internet_message_id": headers.get("messageId"),
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
