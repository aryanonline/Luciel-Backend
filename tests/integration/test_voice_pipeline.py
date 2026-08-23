"""Voice turn pipeline end-to-end through the Arc 14 orchestrator (Architecture §3.1.2).

Hermetic: deterministic STT/TTS (EchoSpeechToText/EchoTextToSpeech) + EchoModel
router + empty retriever — no paid-vendor SDK runs. Covers:
  - start_call ALWAYS logs the mandatory disclosure (VoiceCallRecord +
    voice_disclosure_played audit) and returns opening TwiML (§3.1.2).
  - paused instance → no handling: spoken "unavailable" + hang up, NO LLM (§3.6.2).
  - at_cap → graceful spoken canned reply, NO LLM (§3.4.1b).
  - STT failure → spoken apology + human handoff (graceful degrade).
  - normal turn → transcribe → orchestrator → shaped spoken reply.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.instance import Instance, LucielState
from app.models.voice import VoiceCallRecord
from app.runtime import budget
from app.runtime.llm_router import LLMRouter
from app.runtime.speech import EchoSpeechToText, EchoTextToSpeech
from app.runtime.testing import EchoModel
from app.runtime.voice_pipeline import (
    VOICE_UNAVAILABLE_REPLY,
    handle_turn,
    start_call,
)
from sqlalchemy import func, select

pytestmark = [pytest.mark.asyncio]

_ACTION_URL = "https://api.vantagemind.com/voice/turn"


def _instance(
    admin: uuid.UUID, iid: uuid.UUID, *, state: LucielState, personality=None
) -> Instance:
    return Instance(
        id=iid,
        admin_id=admin,
        name="L",
        state=state,
        channels=["voice"],
        tools=[],
        escalation={},
        personality=personality or {},
    )


def _router() -> LLMRouter:
    echo = EchoModel(confidence=0.9)
    return LLMRouter(primary=echo, fallback=echo)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_start_call_always_logs_mandatory_disclosure():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        inst = _instance(admin, iid, state=LucielState.active)
        s.add(inst)
        await s.flush()
        twiml = await start_call(
            s,
            instance=inst,
            admin_id=admin,
            instance_id=iid,
            provider_call_id="CA_open",
            from_number="+15557654321",
            action_url=_ACTION_URL,
        )
        # Opening TwiML carries the disclosure before the gather (§3.1.2).
        assert "AI assistant" in twiml
        assert twiml.index('<Say voice="Polly.Joanna-Neural">') < twiml.index("<Gather")

        # A VoiceCallRecord proving disclosure delivery exists, with a played timestamp.
        rec = (
            await s.execute(
                select(VoiceCallRecord).where(VoiceCallRecord.provider_call_id == "CA_open")
            )
        ).scalar_one()
        assert rec.disclosure_played_at is not None

        # The voice_disclosure_played audit event was emitted (§3.1.2 auditable consent).
        n = (
            await s.execute(
                select(func.count())
                .select_from(AdminAuditLog)
                .where(AdminAuditLog.event_type == "voice_disclosure_played")
            )
        ).scalar_one()
        assert n == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_start_call_custom_intro_still_carries_mandatory_facts():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        inst = _instance(
            admin,
            iid,
            state=LucielState.active,
            personality={"voiceDisclosureIntro": "Thanks for calling Acme."},
        )
        s.add(inst)
        await s.flush()
        twiml = await start_call(
            s,
            instance=inst,
            admin_id=admin,
            instance_id=iid,
            provider_call_id="CA_custom",
            from_number="+15557654321",
            action_url=_ACTION_URL,
        )
        # Brand intro present AND the mandatory facts still ship (§3.1.2 cannot disable).
        assert "Thanks for calling Acme." in twiml
        assert "AI assistant" in twiml


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_paused_instance_no_handling_speaks_unavailable_no_llm():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        inst = _instance(admin, iid, state=LucielState.paused)
        s.add(inst)
        await s.flush()
        # A router whose provider would error if called — proves NO LLM call happens.
        boom = EchoModel(raise_provider_error=True)
        result = await handle_turn(
            s,
            instance=inst,
            admin_id=admin,
            instance_id=iid,
            from_number="+15557654321",
            speech_audio_ref="are you open?",
            action_url=_ACTION_URL,
            stt=EchoSpeechToText(),
            tts=EchoTextToSpeech(),
            router=LLMRouter(primary=boom, fallback=boom),
            has_payment_method=False,
        )
        assert result.paused is True
        assert result.spoken_text == VOICE_UNAVAILABLE_REPLY
        assert "<Hangup/>" in result.twiml
        assert result.counted is False


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_stt_failure_degrades_to_apology_and_handoff():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        inst = _instance(admin, iid, state=LucielState.active)
        s.add(inst)
        await s.flush()
        result = await handle_turn(
            s,
            instance=inst,
            admin_id=admin,
            instance_id=iid,
            from_number="+15557654321",
            speech_audio_ref="anything",
            action_url=_ACTION_URL,
            stt=EchoSpeechToText(raise_error=True),
            tts=EchoTextToSpeech(),
            router=_router(),
            has_payment_method=False,
        )
        assert result.stt_failed is True
        assert "<Hangup/>" in result.twiml
        assert result.counted is False


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_at_cap_speaks_graceful_reply_without_llm():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        inst = _instance(admin, iid, state=LucielState.active)
        s.add(inst)
        await s.flush()
        # Drive the tenant to the cap so the orchestrator returns the no-LLM at_cap reply.
        state0 = await budget.check_budget(s, admin_id=admin, instance_id=iid)
        for _ in range(state0.free_allowance):
            await budget.count_session_once(
                s, admin_id=admin, instance_id=iid, session_id=uuid.uuid4()
            )
        result = await handle_turn(
            s,
            instance=inst,
            admin_id=admin,
            instance_id=iid,
            from_number="+15557654321",
            speech_audio_ref="hello",
            action_url=_ACTION_URL,
            stt=EchoSpeechToText(),
            tts=EchoTextToSpeech(),
            router=_router(),
            has_payment_method=False,
        )
        assert result.at_cap is True
        assert result.counted is False
        assert "<Hangup/>" in result.twiml  # nothing more for the AI to say


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_normal_turn_runs_through_orchestrator_and_speaks_reply():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        inst = _instance(admin, iid, state=LucielState.active)
        s.add(inst)
        await s.flush()
        result = await handle_turn(
            s,
            instance=inst,
            admin_id=admin,
            instance_id=iid,
            from_number="+15557654321",
            speech_audio_ref="do you have any openings?",
            action_url=_ACTION_URL,
            stt=EchoSpeechToText(),
            tts=EchoTextToSpeech(),
            router=_router(),
            has_payment_method=False,
        )
        assert result.paused is False and result.stt_failed is False
        assert result.spoken_text  # a spoken reply was produced
        assert result.counted is True
        # Normal turn keeps the line open to gather the next utterance.
        assert "<Gather" in result.twiml


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_hosted_synthesis_upgrades_the_reply_to_play():
    """The §3.1.2 hosting layer end-to-end through the pipeline: Echo synthesis →
    LocalVoiceAudioStore upload → presigned URL → <Play> TwiML, hermetically."""
    from app.runtime.voice_audio import HostedAudioTts, LocalVoiceAudioStore

    LocalVoiceAudioStore.reset()
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        inst = _instance(admin, iid, state=LucielState.active)
        s.add(inst)
        await s.flush()
        result = await handle_turn(
            s,
            instance=inst,
            admin_id=admin,
            instance_id=iid,
            from_number="+15557654321",
            speech_audio_ref="do you have any openings?",
            action_url=_ACTION_URL,
            stt=EchoSpeechToText(),
            tts=HostedAudioTts(
                inner=EchoTextToSpeech(),
                store=LocalVoiceAudioStore(),
                admin_id=admin,
                instance_id=iid,
                call_sid="CA_hosted",
                ttl_seconds=900,
            ),
            router=_router(),
            has_payment_method=False,
        )
        assert result.counted is True  # a played reply is still one billed turn
        assert result.audio_url is not None
        assert result.audio_url.startswith(f"https://voice-audio.local/vm/{admin}/voice/")
        assert "<Play>" in result.twiml and "<Say" not in result.twiml
        # The presigned query's & separators are escaped into valid XML.
        assert "&amp;" in result.twiml
    LocalVoiceAudioStore.reset()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_hosting_failure_degrades_to_say_and_still_serves_the_turn():
    """Never a dead call: the store exploding mid-upload demotes the reply to the
    honest <Say> of the same text — the turn is served and billed normally."""
    from app.runtime.voice_audio import HostedAudioTts

    class ExplodingStore:
        async def put_and_presign(self, *, key: str, data: bytes, ttl_seconds: int) -> str:
            raise ConnectionError("s3 down")

    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        inst = _instance(admin, iid, state=LucielState.active)
        s.add(inst)
        await s.flush()
        result = await handle_turn(
            s,
            instance=inst,
            admin_id=admin,
            instance_id=iid,
            from_number="+15557654321",
            speech_audio_ref="hello there",
            action_url=_ACTION_URL,
            stt=EchoSpeechToText(),
            tts=HostedAudioTts(
                inner=EchoTextToSpeech(),
                store=ExplodingStore(),
                admin_id=admin,
                instance_id=iid,
                call_sid="CA_broken",
                ttl_seconds=900,
            ),
            router=_router(),
            has_payment_method=False,
        )
        assert result.audio_url is None
        assert '<Say voice="Polly.Joanna-Neural">' in result.twiml and "<Play>" not in result.twiml
        assert result.counted is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_turns_of_one_call_share_a_session_and_a_second_call_opens_a_new_one():
    """The CALL is the conversation (owner decision 2026-08-18): two turns with one
    CallSid share a session; a second CallSid from the SAME caller opens a NEW
    session and bills a NEW conversation — three calls never collapse into one row."""
    from app.models.budget import CountedSession
    from app.models.conversation import Session

    admin, iid = uuid.uuid4(), uuid.uuid4()
    caller = "+15557654321"

    async def turn(call_sid: str, text: str):
        async with tenant_session(admin) as s:
            inst = (
                await s.execute(select(Instance).where(Instance.id == iid))
            ).scalar_one_or_none()
            if inst is None:
                inst = _instance(admin, iid, state=LucielState.active)
                s.add(inst)
                await s.flush()
            return await handle_turn(
                s,
                instance=inst,
                admin_id=admin,
                instance_id=iid,
                from_number=caller,
                speech_audio_ref=text,
                action_url=_ACTION_URL,
                stt=EchoSpeechToText(),
                tts=EchoTextToSpeech(),
                router=_router(),
                has_payment_method=False,
                provider_call_id=call_sid,
            )

    await turn("CA_first", "do you have any openings?")
    await turn("CA_first", "how about tuesday?")
    await turn("CA_second", "hi, me again — one more question")

    async with tenant_session(admin) as s:
        sessions = (
            await s.execute(
                select(func.count()).select_from(Session).where(Session.admin_id == admin)
            )
        ).scalar_one()
        counted = (
            await s.execute(
                select(func.count())
                .select_from(CountedSession)
                .where(CountedSession.admin_id == admin)
            )
        ).scalar_one()
    assert int(sessions) == 2  # one per CALL, not one per caller number
    assert int(counted) == 2  # each call is its own billed conversation


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_voice_turn_threads_the_instance_persona_and_context():
    """Live-caught 2026-08-18: voice built the Orchestrator bare — generic persona,
    no business context, no configured assistant name (the c14 persona fix never
    reached the phone). The system prompt on a voice turn must carry both."""
    captured: list[str] = []

    class RecordingModel:
        name = "recorder"

        async def complete(self, system_prompt, messages):
            captured.append(system_prompt)
            from app.runtime.testing import EchoModel

            return await EchoModel(confidence=0.9).complete(system_prompt, messages)

    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        inst = _instance(
            admin,
            iid,
            state=LucielState.active,
            personality={
                "assistantName": "Sunny",
                "businessContext": "North Shore Plumbing: 24h emergency service.",
            },
        )
        s.add(inst)
        await s.flush()
        rec = RecordingModel()
        await handle_turn(
            s,
            instance=inst,
            admin_id=admin,
            instance_id=iid,
            from_number="+15557654321",
            speech_audio_ref="do you have any openings?",
            action_url=_ACTION_URL,
            stt=EchoSpeechToText(),
            tts=EchoTextToSpeech(),
            router=LLMRouter(primary=rec, fallback=rec),
            has_payment_method=False,
            provider_call_id="CA_persona",
        )
    assert captured, "the model was not called"
    system_text = captured[0]
    assert "Sunny" in system_text  # configured assistant name reached the prompt
    assert "North Shore Plumbing" in system_text  # business context reached the prompt


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_voice_caller_number_alone_writes_the_lead():
    """Identity IS capture on the phone (live-caught: the escalation email said
    'Contact: not captured' about a caller whose number we knew the whole time).
    A pleasantry from a known number still writes/keeps the Lead row — with no
    fabricated intent."""
    from app.models.lead import Lead

    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        inst = _instance(admin, iid, state=LucielState.active)
        s.add(inst)
        await s.flush()
        await handle_turn(
            s,
            instance=inst,
            admin_id=admin,
            instance_id=iid,
            from_number="+15557654321",
            speech_audio_ref="No its alright thankyou",
            action_url=_ACTION_URL,
            stt=EchoSpeechToText(),
            tts=EchoTextToSpeech(),
            router=_router(),
            has_payment_method=False,
            provider_call_id="CA_lead",
        )
        lead = (await s.execute(select(Lead).where(Lead.admin_id == admin))).scalar_one()
        assert lead.contact_identifier == "+15557654321"
        assert lead.intent is None  # a pleasantry is not an "intent"
