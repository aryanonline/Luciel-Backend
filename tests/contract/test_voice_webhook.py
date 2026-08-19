"""Voice ingress contract tests (Arc 14b — §3.1.2/§3.1.3/§3.4.1b/§3.6.2).

The voice pipeline (adapter, disclosure, TwiML, call records) was fully built and
fully unreachable: no webhook route existed, so an enabled Voice channel answered
nothing while showing Connected. These pin the ingress that closes that:

  - signature verified BEFORE tenant lookup; a bad signature is never routed and
    still answers valid TwiML (dead air is not an ack);
  - call start plays the mandatory disclosure BEFORE gathering caller audio and
    logs it (VoiceCallRecord + voice_disclosure_played);
  - a disabled Voice channel / paused Luciel never claims to answer (§3.8.7 A /
    §3.6.2) — spoken unavailable + hangup, no disclosure record;
  - a caller turn runs the SAME runtime as every channel and replies as TwiML;
  - a silent Gather timeout reprompts WITHOUT consuming conversation budget.
"""

from __future__ import annotations

import uuid

import pytest
from app.channels.number_operability import NullOperabilityProbe
from app.channels.phone_lifecycle import attach_sms_number
from app.channels.signatures import twilio_signature
from app.config import get_settings
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.budget import CountedSession
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.voice import VoiceCallRecord
from sqlalchemy import func, select

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

BYO_NUMBER = "+14155550188"
CALLER = "+16045550107"
VOICE_URL = "http://test/api/v1/webhooks/twilio/voice"
TURN_URL = "http://test/api/v1/webhooks/twilio/voice/turn"


async def _seed(voice_enabled: bool = True, state: LucielState = LucielState.active):
    admin = uuid.uuid4()
    iid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin}@x.com",
                email_lower=f"{admin}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="Sarah Chen",
                state=state,
                embed_key_public_id=f"vm_live_{admin.hex[:8]}",
                channels=[
                    {"id": "sms", "enabled": True},
                    {"id": "voice", "enabled": voice_enabled},
                ],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
    async with tenant_session(admin) as s:
        await attach_sms_number(
            s,
            admin_id=admin,
            instance_id=iid,
            e164_number=BYO_NUMBER,
            probe=NullOperabilityProbe(operable_numbers={BYO_NUMBER}),
        )
    return admin, iid


def _signed(url: str, params: dict[str, str], auth_token: str) -> dict[str, str]:
    return {"X-Twilio-Signature": twilio_signature(auth_token, url, params)}


async def _call_records(admin: uuid.UUID) -> int:
    async with tenant_session(admin) as s:
        return int(
            (
                await s.execute(
                    select(func.count())
                    .select_from(VoiceCallRecord)
                    .where(VoiceCallRecord.admin_id == admin)
                )
            ).scalar_one()
        )


async def test_bad_signature_is_never_routed_but_still_answers_twiml(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "twilio_auth_token", "authtok", raising=False)
    r = await client.post(
        "/api/v1/webhooks/twilio/voice",
        data={"From": CALLER, "To": BYO_NUMBER, "CallSid": "CA1"},
        headers={"X-Twilio-Signature": "wrong"},
    )
    assert r.status_code == 200
    assert "<Hangup/>" in r.text  # valid TwiML, not dead air; call is not handled


async def test_call_start_plays_disclosure_before_gather_and_logs_it(client, monkeypatch):
    auth_token = "authtok-voice"
    monkeypatch.setattr(get_settings(), "twilio_auth_token", auth_token, raising=False)
    admin, _ = await _seed()

    params = {"From": CALLER, "To": BYO_NUMBER, "CallSid": "CA-start-1"}
    r = await client.post(
        "/api/v1/webhooks/twilio/voice", data=params, headers=_signed(VOICE_URL, params, auth_token)
    )
    assert r.status_code == 200
    body = r.text
    # Mandatory disclosure BEFORE any caller audio is captured (§3.1.2).
    assert "AI assistant" in body and "not a human" in body
    assert "recorded and transcribed" in body
    assert body.index('<Say voice="Polly.Joanna-Neural">') < body.index("<Gather")
    assert 'action="http://test/api/v1/webhooks/twilio/voice/turn"' in body

    assert await _call_records(admin) == 1
    async with tenant_session(admin) as s:
        events = (
            (
                await s.execute(
                    select(AdminAuditLog.event_type).where(AdminAuditLog.admin_id == admin)
                )
            )
            .scalars()
            .all()
        )
    assert "voice_disclosure_played" in events


async def test_disabled_voice_channel_never_claims_to_answer(client, monkeypatch):
    auth_token = "authtok-voice"
    monkeypatch.setattr(get_settings(), "twilio_auth_token", auth_token, raising=False)
    admin, _ = await _seed(voice_enabled=False)

    params = {"From": CALLER, "To": BYO_NUMBER, "CallSid": "CA-off-1"}
    r = await client.post(
        "/api/v1/webhooks/twilio/voice", data=params, headers=_signed(VOICE_URL, params, auth_token)
    )
    assert r.status_code == 200
    assert "isn't taking calls right now" in r.text and "<Hangup/>" in r.text
    assert await _call_records(admin) == 0  # no disclosure record for an unhandled call


async def test_paused_luciel_is_paused_on_voice_too(client, monkeypatch):
    auth_token = "authtok-voice"
    monkeypatch.setattr(get_settings(), "twilio_auth_token", auth_token, raising=False)
    admin, _ = await _seed(state=LucielState.paused)

    params = {"From": CALLER, "To": BYO_NUMBER, "CallSid": "CA-paused-1"}
    r = await client.post(
        "/api/v1/webhooks/twilio/voice", data=params, headers=_signed(VOICE_URL, params, auth_token)
    )
    assert r.status_code == 200
    assert "<Hangup/>" in r.text
    assert await _call_records(admin) == 0


async def test_a_caller_turn_runs_the_runtime_and_replies_as_twiml(client, monkeypatch):
    auth_token = "authtok-voice"
    monkeypatch.setattr(get_settings(), "twilio_auth_token", auth_token, raising=False)
    admin, _ = await _seed()

    params = {
        "From": CALLER,
        "To": BYO_NUMBER,
        "CallSid": "CA-turn-1",
        "SpeechResult": "Are you taking new clients this month?",
    }
    r = await client.post(
        "/api/v1/webhooks/twilio/voice/turn",
        data=params,
        headers=_signed(TURN_URL, params, auth_token),
    )
    assert r.status_code == 200
    assert '<Say voice="Polly.Joanna-Neural">' in r.text
    # The turn ran the real budget path: one billed session, counted once.
    async with tenant_session(admin) as s:
        counted = (
            await s.execute(
                select(func.count())
                .select_from(CountedSession)
                .where(CountedSession.admin_id == admin)
            )
        ).scalar_one()
    assert int(counted) == 1


async def test_hosted_synthesis_answers_play_twiml_and_still_bills_one_session(client, monkeypatch):
    """The §3.1.2 hosting layer on the wire: with a hosted delivery bound, the turn
    answers <Play> with an escaped presigned URL — and the budget path is identical
    to the <Say> turn (a nicer voice is not a second conversation)."""
    from app.runtime import voice_audio
    from app.runtime.speech import EchoTextToSpeech
    from app.runtime.voice_audio import HostedAudioTts, LocalVoiceAudioStore

    auth_token = "authtok-voice"
    monkeypatch.setattr(get_settings(), "twilio_auth_token", auth_token, raising=False)
    admin, iid = await _seed()
    LocalVoiceAudioStore.reset()

    def hosted(*, admin_id, instance_id, call_sid):
        return HostedAudioTts(
            inner=EchoTextToSpeech(),
            store=LocalVoiceAudioStore(),
            admin_id=admin_id,
            instance_id=instance_id,
            call_sid=call_sid,
            ttl_seconds=900,
        )

    monkeypatch.setattr(voice_audio, "build_hosted_voice_tts", hosted)

    params = {
        "From": CALLER,
        "To": BYO_NUMBER,
        "CallSid": "CA-hosted-1",
        "SpeechResult": "Are you taking new clients this month?",
    }
    r = await client.post(
        "/api/v1/webhooks/twilio/voice/turn",
        data=params,
        headers=_signed(TURN_URL, params, auth_token),
    )
    assert r.status_code == 200
    assert "<Play>" in r.text
    assert f"https://voice-audio.local/vm/{admin}/voice/{iid}/CA-hosted-1/" in r.text
    assert "&amp;" in r.text  # presigned query separators escaped into valid XML
    async with tenant_session(admin) as s:
        counted = (
            await s.execute(
                select(func.count())
                .select_from(CountedSession)
                .where(CountedSession.admin_id == admin)
            )
        ).scalar_one()
    assert int(counted) == 1
    LocalVoiceAudioStore.reset()


async def test_mock_default_still_answers_say_never_a_fake_play(client, monkeypatch):
    """Regression: in mock posture (no live mode, no bucket) the delivery builder
    answers None and the turn speaks via Twilio's <Say> — a Local store's fake URL
    must never be rendered into real TwiML."""
    auth_token = "authtok-voice"
    monkeypatch.setattr(get_settings(), "twilio_auth_token", auth_token, raising=False)
    admin, _ = await _seed()

    params = {
        "From": CALLER,
        "To": BYO_NUMBER,
        "CallSid": "CA-mockdefault-1",
        "SpeechResult": "What are your hours?",
    }
    r = await client.post(
        "/api/v1/webhooks/twilio/voice/turn",
        data=params,
        headers=_signed(TURN_URL, params, auth_token),
    )
    assert r.status_code == 200
    assert '<Say voice="Polly.Joanna-Neural">' in r.text and "<Play>" not in r.text


async def test_a_silent_gather_timeout_reprompts_without_burning_budget(client, monkeypatch):
    auth_token = "authtok-voice"
    monkeypatch.setattr(get_settings(), "twilio_auth_token", auth_token, raising=False)
    admin, _ = await _seed()

    params = {"From": CALLER, "To": BYO_NUMBER, "CallSid": "CA-silence-1"}
    r = await client.post(
        "/api/v1/webhooks/twilio/voice/turn",
        data=params,
        headers=_signed(TURN_URL, params, auth_token),
    )
    assert r.status_code == 200
    assert "didn't catch that" in r.text and "<Gather" in r.text
    async with tenant_session(admin) as s:
        counted = (
            await s.execute(
                select(func.count())
                .select_from(CountedSession)
                .where(CountedSession.admin_id == admin)
            )
        ).scalar_one()
    assert int(counted) == 0


STATUS_URL = "http://test/api/v1/webhooks/twilio/voice/status"


async def _turn(client, auth_token: str, call_sid: str, speech: str) -> None:
    params = {"From": CALLER, "To": BYO_NUMBER, "CallSid": call_sid, "SpeechResult": speech}
    r = await client.post(
        "/api/v1/webhooks/twilio/voice/turn",
        data=params,
        headers=_signed(TURN_URL, params, auth_token),
    )
    assert r.status_code == 200


async def test_two_calls_from_the_same_number_are_two_sessions_and_two_billed(client, monkeypatch):
    """Each CALL is one conversation (owner decision 2026-08-18, live-caught: three
    separate calls collapsed into one dashboard row and one billed conversation).
    Two CallSids from the SAME caller inside the old 30-minute window must open two
    sessions and count two conversations."""
    from app.models.conversation import Session

    auth_token = "authtok-voice"
    monkeypatch.setattr(get_settings(), "twilio_auth_token", auth_token, raising=False)
    admin, _ = await _seed()

    await _turn(client, auth_token, "CA-call-one", "Do you replace water heaters?")
    await _turn(client, auth_token, "CA-call-two", "What are your hours?")

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
    assert int(sessions) == 2
    assert int(counted) == 2


async def test_status_callback_finalizes_the_calls_session_at_hangup(client, monkeypatch):
    """The voice twin of the widget close beacon: a terminal CallStatus stamps
    ended_at NOW and writes the summary immediately — the dashboard row stops
    reading as in-progress at hangup, not at the next hourly sweep."""
    from app.models.conversation import Session

    auth_token = "authtok-voice"
    monkeypatch.setattr(get_settings(), "twilio_auth_token", auth_token, raising=False)
    admin, _ = await _seed()

    # The REAL call flow: call start writes the VoiceCallRecord (disclosure), the
    # turn opens the session and joins it onto the record, the callback ends it.
    open_params = {"From": CALLER, "To": BYO_NUMBER, "CallSid": "CA-hangup-1"}
    r0 = await client.post(
        "/api/v1/webhooks/twilio/voice",
        data=open_params,
        headers=_signed(VOICE_URL, open_params, auth_token),
    )
    assert r0.status_code == 200
    await _turn(client, auth_token, "CA-hangup-1", "Do you serve North Vancouver?")

    params = {
        "From": CALLER,
        "To": BYO_NUMBER,
        "CallSid": "CA-hangup-1",
        "CallStatus": "completed",
    }
    r = await client.post(
        "/api/v1/webhooks/twilio/voice/status",
        data=params,
        headers=_signed(STATUS_URL, params, auth_token),
    )
    assert r.status_code == 200
    async with tenant_session(admin) as s:
        sess = (
            await s.execute(select(Session).where(Session.admin_id == admin))
        ).scalar_one()
        assert sess.ended_at is not None
        assert sess.summary  # summarized at the REAL call end, not the sweep
    # Idempotent: a duplicate callback is a quiet ack, nothing double-finalized.
    r2 = await client.post(
        "/api/v1/webhooks/twilio/voice/status",
        data=params,
        headers=_signed(STATUS_URL, params, auth_token),
    )
    assert r2.status_code == 200


async def test_status_callback_with_a_bad_signature_finalizes_nothing(client, monkeypatch):
    from app.models.conversation import Session

    auth_token = "authtok-voice"
    monkeypatch.setattr(get_settings(), "twilio_auth_token", auth_token, raising=False)
    admin, _ = await _seed()

    open_params = {"From": CALLER, "To": BYO_NUMBER, "CallSid": "CA-badsig-1"}
    r0 = await client.post(
        "/api/v1/webhooks/twilio/voice",
        data=open_params,
        headers=_signed(VOICE_URL, open_params, auth_token),
    )
    assert r0.status_code == 200
    await _turn(client, auth_token, "CA-badsig-1", "Do you do emergency call-outs?")

    r = await client.post(
        "/api/v1/webhooks/twilio/voice/status",
        data={"From": CALLER, "To": BYO_NUMBER, "CallSid": "CA-badsig-1", "CallStatus": "completed"},
        headers={"X-Twilio-Signature": "forged"},
    )
    assert r.status_code == 403
    async with tenant_session(admin) as s:
        sess = (
            await s.execute(select(Session).where(Session.admin_id == admin))
        ).scalar_one()
        assert sess.ended_at is None  # the forged callback ended nothing


async def test_status_callback_for_an_unknown_call_is_a_quiet_ack(client, monkeypatch):
    auth_token = "authtok-voice"
    monkeypatch.setattr(get_settings(), "twilio_auth_token", auth_token, raising=False)
    await _seed()

    params = {
        "From": CALLER,
        "To": BYO_NUMBER,
        "CallSid": "CA-never-existed",
        "CallStatus": "completed",
    }
    r = await client.post(
        "/api/v1/webhooks/twilio/voice/status",
        data=params,
        headers=_signed(STATUS_URL, params, auth_token),
    )
    assert r.status_code == 200
