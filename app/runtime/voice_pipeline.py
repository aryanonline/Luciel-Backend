"""Voice turn pipeline — disclosure → STT → agentic loop → voice shaping → TTS.

Ties the Voice channel adapter (app/channels/voice_adapter.py) to the Arc 14 runtime
(orchestrator + budget gate). A voice turn flows through the SAME orchestrator and
budget gate as every other channel (one call/session = one budget unit, §3.4.1b) —
voice does not get a parallel runtime.

Two entry points mirror the two call phases:

  start_call (call connects):
    - ALWAYS emit the mandatory recording/AI disclosure FIRST (Legal §3.1.2 /
      Architecture §3.1.2), before any caller audio is transcribed. This writes the
      VoiceCallRecord (disclosure_played_at) + a voice_disclosure_played audit event,
      then returns opening TwiML. There is NO code path that skips this — even a
      paused instance still receives a (paused) disclosure-free terminal message only
      because no call is handled at all (see below); when a call IS handled, the
      disclosure is unconditional.

  handle_turn (caller spoke):
    - paused instance → no handling: a brief spoken "unavailable" + hang up, no STT of
      content for reasoning, no LLM (§3.6.2 paused → no handling).
    - at_cap → graceful spoken canned reply, NO LLM call (§3.4.1b); the orchestrator
      already enforces this and we speak its AT_CAP reply.
    - otherwise → transcribe (STT) → orchestrator.run_turn → shape for voice → TTS.

Voice shaping (§3.4.2): spoken responses are short and carry NO markdown; shape_for_voice
strips markup and bounds length so the channel arbiter's voice constraints hold.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.channels.voice_adapter import (
    build_disclosure,
    disclosure_contains_mandatory_facts,
    opening_twiml,
    reply_twiml,
)
from app.models.audit import ActorType
from app.models.instance import Instance, LucielState
from app.models.voice import VoiceCallRecord
from app.runtime.knowledge_retrieval import make_retriever
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import AT_CAP_REPLY, Orchestrator
from app.runtime.sessions import resolve_or_open_session
from app.runtime.speech import SpeechError, SpeechToText, TextToSpeech

# Voice replies stay short — long answers switch channel or get truncated for speech
# (§3.4.2 voice shaping). A hard spoken-length ceiling keeps calls within cost bounds.
VOICE_REPLY_MAX_CHARS = 600

# Spoken message when the instance is paused/soft-deleted — no handling (§3.6.2).
VOICE_UNAVAILABLE_REPLY = "Sorry, this line isn't taking calls right now. Please try again later."

_MARKDOWN_RE = re.compile(r"[*_`#>\[\]]+")


def shape_for_voice(text: str) -> str:
    """Strip markdown and bound length for spoken delivery (§3.4.2 voice shaping)."""
    cleaned = _MARKDOWN_RE.sub("", text).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) > VOICE_REPLY_MAX_CHARS:
        cleaned = cleaned[: VOICE_REPLY_MAX_CHARS - 1].rstrip() + "…"
    return cleaned


def _is_paused(inst: Instance) -> bool:
    return inst.state in (
        LucielState.paused,
        LucielState.luciel_grace_window,
        LucielState.luciel_hard_deleted,
    )


@dataclass
class VoiceTurnResult:
    twiml: str
    spoken_text: str
    counted: bool = False
    at_cap: bool = False
    handed_off: bool = False
    paused: bool = False
    stt_failed: bool = False


async def start_call(
    session: AsyncSession,
    *,
    instance: Instance,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    provider_call_id: str,
    from_number: str,
    action_url: str,
) -> str:
    """Call connected: ALWAYS play the mandatory disclosure first (§3.1.2), logged.

    Returns the opening TwiML (disclosure <Say> then <Gather>). The disclosure wording
    may be admin-customized via personality, but build_disclosure always carries the
    AI-identity + recording facts (Defense 6 §3.4.14) — asserted here so a stripped
    customization can never be played.
    """
    custom_intro = None
    personality = instance.personality or {}
    raw = personality.get("voiceDisclosureIntro")
    if isinstance(raw, str) and raw.strip():
        custom_intro = raw

    disclosure = build_disclosure(custom_intro)
    # Hard guarantee: the played notice always contains both mandatory facts (§3.1.2).
    # build_disclosure composes them unconditionally, so this never fails; it stands as
    # an in-code invariant guarding against future edits that might weaken the builder.
    if not disclosure_contains_mandatory_facts(disclosure):
        # Unreachable while build_disclosure is intact; fail loud rather than play a
        # disclosure missing a mandatory fact (§3.1.2 — disclosure cannot be disabled).
        raise RuntimeError("voice disclosure missing mandatory AI-identity/recording fact")

    # Log disclosure delivery BEFORE returning TwiML (§3.1.2 auditable consent posture).
    session.add(
        VoiceCallRecord(
            id=uuid.uuid4(),
            admin_id=admin_id,
            instance_id=instance_id,
            provider_call_id=provider_call_id,
            from_number=from_number,
        )
    )
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        event_type="voice_disclosure_played",
        actor_type=ActorType.platform,
        # No PII: call id + channel only (§3.4.11).
        payload={"channel": "voice", "call_sid": provider_call_id},
    )
    await session.flush()
    return opening_twiml(disclosure, action_url)


async def handle_turn(
    session: AsyncSession,
    *,
    instance: Instance,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    from_number: str,
    speech_audio_ref: str,
    action_url: str,
    stt: SpeechToText,
    tts: TextToSpeech,
    router: LLMRouter,
    has_payment_method: bool = False,
) -> VoiceTurnResult:
    """One caller utterance → spoken reply, through the Arc 14 orchestrator (§3.4.1)."""
    # Paused / soft-deleted → no handling (§3.6.2): speak unavailable + hang up, no LLM.
    if _is_paused(instance):
        await tts.synthesize(VOICE_UNAVAILABLE_REPLY)
        return VoiceTurnResult(
            twiml=reply_twiml(VOICE_UNAVAILABLE_REPLY, action_url, hang_up=True),
            spoken_text=VOICE_UNAVAILABLE_REPLY,
            paused=True,
        )

    # Transcribe inbound caller audio (STT). A provider failure degrades gracefully:
    # speak an apology + hand the caller to a person rather than dropping the call.
    try:
        transcription = await stt.transcribe(speech_audio_ref)
    except SpeechError:
        apology = (
            "I'm sorry, I'm having trouble hearing you. Let me connect you with "
            "someone who can help."
        )
        await tts.synthesize(apology)
        return VoiceTurnResult(
            twiml=reply_twiml(apology, action_url, hang_up=True),
            spoken_text=apology,
            stt_failed=True,
        )

    # Voice identity is STRONG (E.164, §3.4.9) but lead linkage is the identity arc's
    # job; here resolved_lead_id stays None so this turn opens/uses a voice session
    # without inheriting another lead's history (the HARD RULE still holds in memory).
    sess, _is_new = await resolve_or_open_session(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        channel="voice",
        resolved_lead_id=None,
    )

    # Arc 16 binds the production hybrid retriever (vector + intent-gated graph, §3.2.1)
    # behind the orchestrator's injected seam — closing the Arc 14b empty "Arc 11/16" stub.
    # Voice runs the SAME orchestrator/retrieval path as every other channel; retrieved
    # content flows through wrap_untrusted in the loop (§3.4.14).
    retriever = make_retriever(session, admin_id=admin_id, instance_id=instance_id)

    orch = Orchestrator(
        session=session,
        router=router,
        retriever=retriever,
        admin_id=admin_id,
        instance_id=instance_id,
        enabled_channels={"voice"},
        has_payment_method=has_payment_method,
    )
    result = await orch.run_turn(
        session_id=sess.id,
        resolved_lead_id=None,
        channel="voice",
        mode=sess.mode,
        inbound_text=transcription.text,
    )

    # at_cap → the orchestrator already produced the graceful no-LLM reply (§3.4.1b);
    # speak it. handed_off / normal → speak the loop's reply, shaped for voice.
    spoken = shape_for_voice(result.reply_text or AT_CAP_REPLY)
    await tts.synthesize(spoken)
    # End the call on at_cap or handoff (nothing more for the AI to say); otherwise
    # gather the next utterance.
    hang_up = result.at_cap or result.handed_off
    return VoiceTurnResult(
        twiml=reply_twiml(spoken, action_url, hang_up=hang_up),
        spoken_text=spoken,
        counted=result.counted,
        at_cap=result.at_cap,
        handed_off=result.handed_off,
    )
