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
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.channels.voice_adapter import (
    build_disclosure,
    disclosure_contains_mandatory_facts,
    opening_twiml,
    reply_twiml,
)
from app.models.audit import ActorType
from app.models.instance import Instance, is_paused
from app.models.voice import VoiceCallRecord
from app.runtime.knowledge_retrieval import make_chunk_attributor, make_retriever
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import AT_CAP_REPLY, Orchestrator
from app.runtime.sessions import (
    effective_config,
    find_open_session,
    participant_key_for,
    resolve_or_open_session,
)
from app.runtime.speech import SpeechError, SpeechToText, TextToSpeech

# Spoken when a person holds the call and Luciel has nothing to relay (§3.4.12): an
# honest hand-off, never a fabricated "I've passed that along" (2026-09-05 audit F057).
VOICE_HANDOFF_LINE = (
    "Thanks for calling. A member of the team has your details and will follow up with you shortly."
)

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


@dataclass
class VoiceTurnResult:
    twiml: str
    spoken_text: str
    counted: bool = False
    at_cap: bool = False
    handed_off: bool = False
    paused: bool = False
    stt_failed: bool = False
    # The hosted <Play> URL when this turn's reply is synthesized audio (§3.1.2
    # hosting layer); None means the reply went out as <Say>. An honest assertion
    # handle — set only from a playable synthesis result, never inferred.
    audio_url: str | None = None


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
    # Undefaulted for the same reason as Orchestrator.has_payment_method (P2-5): a
    # forgotten kwarg must be a TypeError, not a silently capped paying caller.
    has_payment_method: bool,
    # The Twilio Call SID, when the caller has it: joins this turn's session onto the
    # VoiceCallRecord opened by start_call (§3.1.2) so the disclosure log and the
    # conversation are one auditable thread. Optional so pre-existing callers/tests
    # keep working; the voice webhook passes it.
    provider_call_id: str | None = None,
) -> VoiceTurnResult:
    """One caller utterance → spoken reply, through the Arc 14 orchestrator (§3.4.1)."""
    # TRUE arrival time for the lead transcript row (reply-time analytics, 2026-08-17).
    received_at = datetime.now(UTC)
    # Paused / soft-deleted → no NEW handling (§3.6.2): speak unavailable + hang up,
    # no LLM. The §3.6.2 carve-out (C8): a CALL already in progress at the pause or
    # delete instant finishes — its session (keyed by the call id) is live, so the
    # drain gate admits it; a fresh call after the pause gets the unavailable reply.
    if is_paused(instance):
        draining = await find_open_session(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            channel="voice",
            participant_key=participant_key_for(admin_id, "voice", provider_call_id or from_number),
        )
        if draining is None:
            speech = await tts.synthesize(VOICE_UNAVAILABLE_REPLY)
            audio_url = speech.audio_ref if speech.playable else None
            return VoiceTurnResult(
                twiml=reply_twiml(
                    VOICE_UNAVAILABLE_REPLY, action_url, hang_up=True, audio_url=audio_url
                ),
                spoken_text=VOICE_UNAVAILABLE_REPLY,
                paused=True,
                audio_url=audio_url,
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
        speech = await tts.synthesize(apology)
        audio_url = speech.audio_ref if speech.playable else None
        return VoiceTurnResult(
            twiml=reply_twiml(apology, action_url, hang_up=True, audio_url=audio_url),
            spoken_text=apology,
            stt_failed=True,
            audio_url=audio_url,
        )

    # Voice identity is STRONG (E.164, §3.4.9) but lead linkage is the identity arc's
    # job; here resolved_lead_id stays None so this turn opens/uses a voice session
    # without inheriting another lead's history (the HARD RULE still holds in memory).
    # The CALL is the conversation (owner decision 2026-08-18, live-caught: three
    # separate calls rendered as one dashboard row): the participant key is the
    # provider call id, so every utterance of ONE call shares one session — and a
    # follow-up CALL is a NEW session and a NEW billed conversation, exactly like a
    # fresh widget tab. §3.4.8's "transport identifier" for voice is the call.
    # The from_number fallback covers callers that supply no call id (tests, future
    # providers without one) — those degrade to number-keyed sessions.
    sess, _is_new = await resolve_or_open_session(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        channel="voice",
        resolved_lead_id=None,
        participant_key=participant_key_for(admin_id, "voice", provider_call_id or from_number),
    )
    if provider_call_id is not None:
        # Join the session onto the call's disclosure record (§3.1.2): set once, never
        # re-pointed — the first turn wins, matching "one call, one session".
        await session.execute(
            update(VoiceCallRecord)
            .where(
                VoiceCallRecord.admin_id == admin_id,
                VoiceCallRecord.instance_id == instance_id,
                VoiceCallRecord.provider_call_id == provider_call_id,
                VoiceCallRecord.session_id.is_(None),
            )
            .values(session_id=sess.id)
        )

    # Arc 16 binds the production hybrid retriever (vector + intent-gated graph, §3.2.1)
    # behind the orchestrator's injected seam — closing the Arc 14b empty "Arc 11/16" stub.
    # Voice runs the SAME orchestrator/retrieval path as every other channel; retrieved
    # content flows through wrap_untrusted in the loop (§3.4.14).
    retriever = make_retriever(session, admin_id=admin_id, instance_id=instance_id)
    # Harmony wave 2, item 2a: real chunk attribution alongside the retriever, same
    # as the text channels -- a voice answer backed by real KB retrieval deserves
    # the same honest evidence trail as a widget/SMS/email answer.
    chunk_attributor = make_chunk_attributor(session, admin_id=admin_id, instance_id=instance_id)

    # The SAME instance context as every text channel (live-caught 2026-08-18: voice
    # built the Orchestrator bare, so calls ran a generic persona with no business
    # context and no configured assistant name — the c14 persona fix never reached
    # the phone). Reuses runtime_entry's helpers instead of a third naming copy.
    from app.channels.runtime_entry import disclosure_business_name, names  # noqa: PLC0415

    effective = effective_config(sess.config_snapshot, instance, always_include_channel="voice")
    _business, assistant = names(instance)
    # Tool wiring (audit round 3, C5 — voice parity): the voice Orchestrator was
    # built with NO tool_resolver, so a caller could never be booked or looked up —
    # every proposed tool resolved to `failed` while the text channels dispatched
    # normally. Voice now mirrors runtime_entry.py exactly: same resolver, same
    # snapshot capability set (§3.8.7 rule E).
    from app.tools.registry import make_tool_resolver  # noqa: PLC0415

    orch = Orchestrator(
        session=session,
        router=router,
        retriever=retriever,
        chunk_attributor=chunk_attributor,
        admin_id=admin_id,
        instance_id=instance_id,
        enabled_channels=effective.enabled_channels,
        business_name=disclosure_business_name(instance),
        assistant_name=assistant,
        business_context=effective.business_context,
        personality=effective.personality,
        has_payment_method=has_payment_method,
        snapshot_tool_ids=effective.snapshot_tool_ids,
        tool_resolver=make_tool_resolver(),
    )
    result = await orch.run_turn(
        session_id=sess.id,
        resolved_lead_id=None,
        channel="voice",
        mode=sess.mode,
        inbound_text=transcription.text,
        received_at=received_at,
        # The caller's number IS their identity (§3.4.9 strong channel) — this is
        # what feeds voice lead capture at all.
        transport_identifier=from_number,
    )

    # at_cap → the orchestrator already produced the graceful no-LLM reply (§3.4.1b);
    # speak it. handed_off / normal → speak the loop's reply, shaped for voice.
    # at_cap → the canned line; nothing to say (a person holds the call, §3.4.12) →
    # an honest hand-off line: a person follows up, Luciel never pretends it relayed
    # anything (audit F057); otherwise the loop's own reply.
    spoken = shape_for_voice(
        result.reply_text or (AT_CAP_REPLY if result.at_cap else VOICE_HANDOFF_LINE)
    )
    # A playable result (the hosting layer minted a <Play> URL) upgrades the reply
    # to the synthesized voice; anything else — including every failure inside the
    # hosting wrapper, which never raises — keeps the honest <Say> of the same text.
    speech = await tts.synthesize(spoken)
    audio_url = speech.audio_ref if speech.playable else None
    # End the call on at_cap or handoff (nothing more for the AI to say); otherwise
    # gather the next utterance.
    hang_up = result.at_cap or result.handed_off
    return VoiceTurnResult(
        twiml=reply_twiml(spoken, action_url, hang_up=hang_up, audio_url=audio_url),
        spoken_text=spoken,
        counted=result.counted,
        at_cap=result.at_cap,
        handed_off=result.handed_off,
        audio_url=audio_url,
    )
