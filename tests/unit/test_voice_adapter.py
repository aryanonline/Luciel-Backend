"""Voice channel adapter: Twilio signature verify, TwiML, mandatory disclosure
(Architecture §3.1.2 / Legal §3.1.2).

The disclosure is PLATFORM-ENFORCED and CANNOT be disabled on any tier (§3.1.2);
build_disclosure always composes the AI-identity (§3.4.16) + recording facts even
under an admin custom intro (Defense 6 §3.4.14). The cannot-be-disabled property is
asserted EXPLICITLY below.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels.base import InboundRejected, InstanceContext
from app.channels.signatures import twilio_signature
from app.channels.voice_adapter import (
    VoiceAdapter,
    build_disclosure,
    disclosure_contains_mandatory_facts,
    opening_twiml,
    reply_twiml,
)
from app.runtime.voice_pipeline import shape_for_voice

_TOKEN = "tw_voice_token"  # noqa: S105 - test literal, not a real secret
_URL = "https://api.vantagemind.com/voice"


def test_shape_for_voice_strips_markdown_and_bounds_length():
    # Voice shaping removes markup and bounds spoken length (§3.4.2).
    shaped = shape_for_voice("**Hi** there [link](x) `code` #h\nfoo")
    assert "*" not in shaped and "[" not in shaped and "`" not in shaped and "#" not in shaped
    assert "\n" not in shaped
    assert len(shape_for_voice("a" * 5000)) <= 600


def test_build_disclosure_always_carries_mandatory_facts_no_intro():
    d = build_disclosure()
    assert disclosure_contains_mandatory_facts(d)
    assert "AI assistant" in d
    assert "recorded" in d or "transcrib" in d.lower()


def test_build_disclosure_prepends_custom_intro_but_keeps_facts():
    # Admin customizes wording (brand voice) — facts are APPENDED, never replaced.
    d = build_disclosure("Welcome to Acme Plumbing.")
    assert d.startswith("Welcome to Acme Plumbing.")
    assert disclosure_contains_mandatory_facts(d)


def test_disclosure_cannot_be_disabled():
    """EXPLICIT: no custom intro (empty, whitespace, or brand-only) can strip the
    mandatory AI-identity + recording facts (§3.1.2 — cannot be disabled on any tier).
    """
    # A custom intro that itself contains NONE of the facts still yields a disclosure
    # that carries them — the builder appends them unconditionally (Defense 6 §3.4.14).
    brand_only = "Thanks for calling, we appreciate your business."
    assert not disclosure_contains_mandatory_facts(brand_only)
    composed = build_disclosure(brand_only)
    assert disclosure_contains_mandatory_facts(composed)

    # Empty / whitespace / None intros all still carry the facts — no path disables them.
    for intro in (None, "", "   ", "\n\t"):
        assert disclosure_contains_mandatory_facts(build_disclosure(intro))


def test_opening_twiml_plays_disclosure_then_gathers_speech():
    twiml = opening_twiml(build_disclosure(), "https://x/voice/turn")
    # Disclosure <Say> precedes the <Gather> so the notice is delivered before any
    # caller audio is captured for transcription (§3.1.2).
    assert twiml.index('<Say voice="Polly.Joanna-Neural">') < twiml.index("<Gather")
    assert 'input="speech"' in twiml
    assert "AI assistant" in twiml


def test_reply_twiml_gather_vs_hangup():
    gather = reply_twiml("We open at nine.", "https://x/voice/turn")
    assert "<Gather" in gather and "<Hangup/>" not in gather
    hung = reply_twiml("Goodbye.", "https://x/voice/turn", hang_up=True)
    assert "<Hangup/>" in hung and "<Gather" not in hung


def test_reply_twiml_escapes_markup():
    # Spoken text is XML-escaped so stray characters cannot break the TwiML document.
    twiml = reply_twiml("Tom & Jerry <here>", "https://x/voice/turn")
    assert "&amp;" in twiml and "&lt;here&gt;" in twiml


def test_reply_twiml_plays_hosted_audio_with_escaped_presigned_query():
    """The <Play> upgrade (§3.1.2 hosting layer). The presigned URL is element
    TEXT, so its query-string `&` separators MUST become &amp; — Twilio unescapes
    them on fetch; a raw `&` is malformed XML and kills the whole response."""
    url = "https://bucket.s3.ca-central-1.amazonaws.com/vm/a/x.mp3?X-Amz-Expires=900&X-Amz-Signature=abc"
    twiml = reply_twiml("We open at nine.", "https://x/voice/turn", audio_url=url)
    assert "<Play>" in twiml and "</Play>" in twiml
    assert "X-Amz-Expires=900&amp;X-Amz-Signature=abc" in twiml
    # The reply is played, not spoken twice: no <Say> alongside the <Play>.
    assert "<Say" not in twiml
    assert "<Gather" in twiml  # still gathers the caller's next utterance

    hung = reply_twiml("Goodbye.", "https://x/voice/turn", hang_up=True, audio_url=url)
    assert "<Play>" in hung and "<Hangup/>" in hung and "<Gather" not in hung


def test_reply_twiml_without_audio_url_is_the_historical_say_output():
    # No audio_url → byte-identical to the pre-hosting <Say> markup, so every
    # mock-mode contract assertion stays true without edits.
    twiml = reply_twiml("We open at nine.", "https://x/voice/turn")
    assert '<Say voice="Polly.Joanna-Neural">We open at nine.</Say>' in twiml
    assert "<Play>" not in twiml


@pytest.mark.asyncio
async def test_verify_inbound_resolves_called_number_and_rejects_bad_signature():
    admin, iid = uuid.uuid4(), uuid.uuid4()

    async def resolve(connection_type: str, destination: str) -> InstanceContext:
        # Voice resolves on the called "To" number as an sms_sender connection
        # (number shared by SMS + Voice, §3.1.4).
        assert connection_type == "sms_sender"
        assert destination == "+15551230000"
        return InstanceContext(admin_id=admin, instance_id=iid)

    params = {
        "To": "+15551230000",
        "From": "+15557654321",
        "CallSid": "CA_test",
        "SpeechResult": "",
    }
    sig = twilio_signature(_TOKEN, _URL, params)
    request = {
        "auth_token": _TOKEN,
        "url": _URL,
        "params": params,
        "x_twilio_signature": sig,
        "resolve": resolve,
    }
    adapter = VoiceAdapter()
    ctx = await adapter.verify_inbound(request)
    assert ctx.admin_id == admin and ctx.instance_id == iid

    bad = dict(request, x_twilio_signature="wrong")
    with pytest.raises(InboundRejected):
        await adapter.verify_inbound(bad)


@pytest.mark.asyncio
async def test_receive_normalizes_speech_result_into_inbound_message():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    ctx = InstanceContext(admin_id=admin, instance_id=iid)
    params = {
        "To": "+15551230000",
        "From": "+15557654321",
        "CallSid": "CA_abc",
        "SpeechResult": "do you have any openings?",
    }
    msg = await VoiceAdapter().receive({"params": params}, ctx)
    assert msg.channel == "voice"
    assert msg.body == "do you have any openings?"
    assert msg.customer_identifier == "+15557654321"
    assert msg.channel_metadata["call_sid"] == "CA_abc"
