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
        assert twiml.index("<Say>") < twiml.index("<Gather")

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
