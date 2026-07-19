"""Voice channel adapter — Twilio Voice inbound + TwiML (Architecture §3.1.2, Arc 14b).

A NEW ChannelAdapter following the SAME Arc 13 contract (app/channels/base.py):
verify_inbound (Twilio HMAC-SHA1 signature, reusing app/channels/signatures.py) →
resolve the called number to a tenant (§3.1.3, like SMS) → receive() normalizes the
caller's transcribed speech into the unified InboundMessage envelope.

MANDATORY recording/AI disclosure (Legal §3.1.2 / Architecture §3.1.2):
  - The disclosure is played FIRST on every inbound call, before any caller audio is
    processed for transcription. It satisfies BOTH the AI-identity disclosure (§3.4.16)
    and the consent-to-record disclosure in one spoken notice.
  - It is PLATFORM-ENFORCED and CANNOT be disabled by any admin or on any tier
    (§3.1.2). The admin may customize the *wording*, but the customization can never
    remove the AI-identity fact or the recording/transcription fact (Defense 6
    §3.4.14): build_disclosure() always composes the mandatory facts into the spoken
    text regardless of the admin's custom prefix. There is no code path that omits it.
  - Disclosure delivery is logged per call (§3.1.2) — the voice pipeline writes a
    VoiceCallRecord (disclosure_played_at) + a voice_disclosure_played audit event.

TwiML: the adapter emits the provider markup. The opening call returns a <Response>
that plays the disclosure then <Gather input="speech"> to capture the caller's first
utterance; subsequent turns speak the loop's reply. Real Twilio REST/media binding is
in deployment; the markup itself is generated + tested here. Twilio's speech
recognition supplies the transcript (SpeechResult), which our injected SpeechToText
Protocol abstracts so the pipeline is vendor-agnostic and hermetically testable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

# escape() below ENCODES our own outbound text into generated TwiML (output escaping); it
# never parses untrusted inbound XML, so the XML-parsing attack class B406 warns about does
# not apply (Twilio posts form params, not an XML body we parse). Hence the targeted nosec.
from xml.sax.saxutils import escape  # nosec B406

from app.channels.base import (
    DeliveryReceipt,
    InboundMessage,
    InboundRejected,
    InstanceContext,
    OutboundMessage,
)
from app.channels.signatures import verify_twilio

CHANNEL_ID = "voice"

# The two facts the disclosure MUST always convey (§3.1.2). These literals are the
# non-removable core: an admin's custom wording is *prepended*, never substituted for
# these, so the AI-identity (§3.4.16) and recording/transcription notice always ship.
_AI_IDENTITY_FACT = (
    "You're speaking with an AI assistant, not a human, and this call is handled by "
    "an automated system."
)
_RECORDING_FACT = "This call may be recorded and transcribed."
# A two-party-consent jurisdiction treats continuing past the notice as the consent
# event (§3.1.2); the caller may ask for a person or hang up to decline.
_CONSENT_TAIL = "If you'd prefer a person, just say so at any time."


def build_disclosure(custom_intro: str | None = None) -> str:
    """Compose the MANDATORY spoken disclosure (§3.1.2).

    The admin may supply ``custom_intro`` to match brand voice, but the AI-identity
    fact and the recording/transcription fact are ALWAYS appended — there is no
    argument and no config that removes them (Defense 6 §3.4.14). This function is the
    single source of the spoken notice; the adapter has no branch that skips it.
    """
    intro = (custom_intro or "").strip()
    parts = [intro] if intro else []
    parts.append(_AI_IDENTITY_FACT)
    parts.append(_RECORDING_FACT)
    parts.append(_CONSENT_TAIL)
    return " ".join(parts)


def disclosure_contains_mandatory_facts(text: str) -> bool:
    """True iff a disclosure string still carries both mandatory facts (§3.1.2).

    Used by the cannot-be-disabled guarantee: any disclosure the platform plays must
    pass this, so a customization that stripped the facts would be rejected upstream.
    """
    lowered = text.lower()
    return (
        "ai assistant" in lowered
        and "not a human" in lowered
        and ("recorded" in lowered or "transcrib" in lowered)
    )


def opening_twiml(disclosure: str, action_url: str) -> str:
    """TwiML for call start: play the mandatory disclosure, THEN gather caller speech.

    The disclosure <Say> precedes the <Gather>, so the notice is delivered before any
    caller audio is captured for transcription (§3.1.2 "before any caller audio is
    processed"). action_url is where Twilio posts the captured SpeechResult.
    """
    said = escape(disclosure)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"<Say>{said}</Say>"
        f'<Gather input="speech" action="{escape(action_url)}" method="POST" '
        'speechTimeout="auto"/>'
        "</Response>"
    )


def reply_twiml(spoken_text: str, action_url: str, *, hang_up: bool = False) -> str:
    """TwiML for a subsequent turn: speak the loop's reply, then gather or hang up.

    Voice responses are short and contain NO markup/markdown (the channel arbiter +
    voice shaping enforce this, §3.4.2); only spoken words are emitted.
    """
    said = escape(spoken_text)
    if hang_up:
        return (
            f'<?xml version="1.0" encoding="UTF-8"?><Response><Say>{said}</Say><Hangup/></Response>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f"<Say>{said}</Say>"
        f'<Gather input="speech" action="{escape(action_url)}" method="POST" '
        'speechTimeout="auto"/>'
        "</Response>"
    )


class VoiceSender:  # pragma: no cover - interface marker
    """Outbound voice is initiated by returning TwiML to Twilio, not a REST push.

    The Voice channel responds in-band (TwiML on the webhook response), so there is no
    separate async send like SMS. send() is provided to satisfy the ChannelAdapter
    Protocol; it formats an outbound spoken turn as TwiML in the receipt detail.

    This is NOT a deferred stub: unlike SMS (which needs a real Twilio REST push,
    see ``app/channels/twilio_sender.py``), Twilio Voice's outbound reply IS the
    TwiML returned on the webhook response — there is no separate outbound call to
    make, so no sender/credential is needed on the voice send path (§3.1.2).
    """


class VoiceAdapter:
    """Twilio Voice inbound adapter (§3.1.2). Follows the Arc 13 ChannelAdapter contract."""

    channel_id = CHANNEL_ID

    async def verify_inbound(self, request: dict[str, Any]) -> InstanceContext:
        auth_token = request["auth_token"]  # from Secrets Manager (Arc 17)
        url = request["url"]
        params = request["params"]  # the Twilio Voice POST params
        signature = request.get("x_twilio_signature", "")
        if not verify_twilio(auth_token, url, params, signature):
            # Unsigned/invalid inbound is rejected, never routed (§3.1.3, §3.7.2).
            raise InboundRejected("twilio_signature_invalid")
        resolve = request["resolve"]
        # Voice resolves on the called number exactly like SMS (§3.1.3): the dedicated
        # provisioned number maps to one tenant. The number is provisioned as an
        # sms_sender connection shared by SMS + Voice (§3.1.4 phone lifecycle).
        to_number = params.get("To", "")
        ctx: InstanceContext = await resolve("sms_sender", to_number)
        return ctx

    async def receive(self, request: dict[str, Any], ctx: InstanceContext) -> InboundMessage:
        params = request["params"]
        # Twilio <Gather input="speech"> posts the recognized transcript as SpeechResult.
        # The injected SpeechToText Protocol abstracts the actual recognition so this is
        # vendor-agnostic; on the opening call SpeechResult is absent (empty body).
        speech = params.get("SpeechResult", "")
        return InboundMessage(
            admin_id=ctx.admin_id,
            instance_id=ctx.instance_id,
            channel=CHANNEL_ID,
            session_id=None,
            customer_identifier=params.get("From", ""),
            body=speech,
            channel_metadata={
                "call_sid": params.get("CallSid"),
                "to_number": params.get("To", ""),
            },
            received_at=datetime.now(UTC),
        )

    async def send(self, message: OutboundMessage) -> DeliveryReceipt:
        # Voice replies are returned in-band as TwiML on the webhook response rather
        # than pushed; the receipt carries the markup for the telephony layer to serve.
        action_url = str(message.metadata.get("action_url", ""))
        hang_up = bool(message.metadata.get("hang_up", False))
        twiml = reply_twiml(message.body, action_url, hang_up=hang_up)
        return DeliveryReceipt(
            accepted=True,
            provider_message_id=str(message.metadata.get("call_sid") or uuid.uuid4()),
            detail=twiml,
        )
