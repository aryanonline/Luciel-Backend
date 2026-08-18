"""Voice audio hosting (§3.1.2 <Play>) — the store seam and the never-a-dead-call rule.

``HostedAudioTts`` upgrades a voice reply from Twilio's ``<Say>`` to the synthesized
``<Play>`` voice — but only when EVERY step succeeded. What these tests pin:

  - the tenant ``vm/{admin_id}/`` key doctrine (§3.8.3) on every object;
  - the binding gate is the CDK-injected env var, never ``integration_mode``
    (the ``get_transcript_store`` rationale);
  - the wrapper NEVER raises: provider failure, a bytes-less provider, and a
    store exception all degrade to a NON-playable result whose text the TwiML
    layer speaks via ``<Say>`` — a worse voice must never become a dead call;
  - the delivery builder answers None (honest <Say>) in mock mode, without a
    bucket, and when the live speech providers cannot bind.
"""

from __future__ import annotations

import re
import uuid

import pytest
from app.runtime import voice_audio
from app.runtime.speech import EchoTextToSpeech
from app.runtime.voice_audio import (
    HostedAudioTts,
    LocalVoiceAudioStore,
    S3VoiceAudioStore,
    build_hosted_voice_tts,
    get_voice_audio_store,
    set_voice_audio_store,
    voice_audio_key_for,
)

pytestmark = [pytest.mark.asyncio]

ADMIN = uuid.uuid4()
INSTANCE = uuid.uuid4()


@pytest.fixture(autouse=True)
def _fresh_store():
    LocalVoiceAudioStore.reset()
    set_voice_audio_store(None)
    yield
    LocalVoiceAudioStore.reset()
    set_voice_audio_store(None)


def _hosted(inner, store) -> HostedAudioTts:
    return HostedAudioTts(
        inner=inner,
        store=store,
        admin_id=ADMIN,
        instance_id=INSTANCE,
        call_sid="CA_test",
        ttl_seconds=900,
    )


def test_object_keys_are_tenant_prefixed_per_call():
    key = voice_audio_key_for(ADMIN, INSTANCE, "CA_abc")
    assert re.fullmatch(rf"vm/{ADMIN}/voice/{INSTANCE}/CA_abc/[0-9a-f-]{{36}}\.mp3", key), key
    # No CallSid (defensive): still a valid, still tenant-prefixed key.
    assert f"vm/{ADMIN}/voice/{INSTANCE}/unknown/" in voice_audio_key_for(ADMIN, INSTANCE, "")


async def test_local_store_roundtrip_and_url_shape():
    store = LocalVoiceAudioStore()
    url = await store.put_and_presign(key="vm/a/voice/b/c/d.mp3", data=b"mp3!", ttl_seconds=900)
    assert LocalVoiceAudioStore.get_bytes("vm/a/voice/b/c/d.mp3") == b"mp3!"
    # Two query params on purpose: any TwiML rendering must escape the `&`.
    assert url.startswith("https://voice-audio.local/vm/a/voice/b/c/d.mp3?")
    assert "&" in url and "X-Amz-Expires=900" in url


async def test_binding_gate_is_the_env_var_not_integration_mode(monkeypatch):
    monkeypatch.delenv("LUCIEL_VOICE_AUDIO_BUCKET", raising=False)
    assert isinstance(get_voice_audio_store(), LocalVoiceAudioStore)

    set_voice_audio_store(None)
    monkeypatch.setenv("LUCIEL_VOICE_AUDIO_BUCKET", "luciel-dev-voice-audio-123")
    bound = get_voice_audio_store()
    assert isinstance(bound, S3VoiceAudioStore)


async def test_happy_path_hosts_the_echo_bytes_and_answers_playable():
    result = await _hosted(EchoTextToSpeech(), LocalVoiceAudioStore()).synthesize("We open at 9.")
    assert result.playable is True
    assert result.text == "We open at 9."
    assert result.audio_ref.startswith(f"https://voice-audio.local/vm/{ADMIN}/voice/")
    key = result.audio_ref.split("voice-audio.local/")[1].split("?")[0]
    assert LocalVoiceAudioStore.get_bytes(key) == b"echo:We open at 9."


async def test_provider_failure_degrades_to_say_never_raises():
    result = await _hosted(EchoTextToSpeech(raise_error=True), LocalVoiceAudioStore()).synthesize(
        "hello"
    )
    assert result.playable is False
    assert result.text == "hello"  # the <Say> fallback speaks exactly this


async def test_bytes_less_provider_degrades_to_say():
    class NoBytesTts:
        name = "no_bytes"

        async def synthesize(self, text: str):
            from app.runtime.speech import SynthesizedSpeech

            return SynthesizedSpeech(audio_ref="tts://legacy/1", text=text)

    result = await _hosted(NoBytesTts(), LocalVoiceAudioStore()).synthesize("hi")
    assert result.playable is False and result.text == "hi"


async def test_store_exception_degrades_to_say_never_raises():
    class ExplodingStore:
        async def put_and_presign(self, *, key: str, data: bytes, ttl_seconds: int) -> str:
            raise ConnectionError("s3 down")

    result = await _hosted(EchoTextToSpeech(), ExplodingStore()).synthesize("hi there")
    assert result.playable is False and result.text == "hi there"


async def test_builder_is_honest_say_in_mock_and_without_a_bucket(monkeypatch):
    # Mock mode (the default settings): no hosted delivery, even with a bucket var.
    monkeypatch.setenv("LUCIEL_VOICE_AUDIO_BUCKET", "luciel-dev-voice-audio-123")
    assert build_hosted_voice_tts(admin_id=ADMIN, instance_id=INSTANCE, call_sid="CA") is None


async def test_builder_is_honest_say_without_the_premium_opt_in(monkeypatch):
    """Live + bucket + keys but NO ``LUCIEL_VOICE_PREMIUM_AUDIO=1``: the deferral
    holds. The CDK wires the bucket unconditionally, so bucket-presence alone had
    re-enabled the deferred hosting layer — a failing paid ElevenLabs round-trip
    on every reply against the unfunded account (live-caught 2026-08-18)."""
    monkeypatch.setenv("LUCIEL_VOICE_AUDIO_BUCKET", "luciel-dev-voice-audio-123")
    monkeypatch.delenv("LUCIEL_VOICE_PREMIUM_AUDIO", raising=False)
    settings = __import__("app.config", fromlist=["get_settings"]).get_settings()
    monkeypatch.setattr(settings, "integration_mode", "live")
    monkeypatch.setattr(
        settings,
        "platform_secret",
        '{"OPENAI_API_KEY": "sk-fake", "ELEVENLABS_API_KEY": "el-fake"}',
    )
    assert build_hosted_voice_tts(admin_id=ADMIN, instance_id=INSTANCE, call_sid="CA") is None


async def test_slow_synthesis_degrades_to_say_within_the_webhook_window():
    """A provider slower than the synthesize timeout is cut off and the reply
    degrades to <Say> — Twilio abandons webhooks at ~15s, so a hung TTS call must
    never be allowed to hold the turn past it (the c11 latency risk, now bound)."""
    import asyncio as _asyncio

    class SlowTts:
        name = "slow"

        async def synthesize(self, text: str):
            await _asyncio.sleep(5)
            raise AssertionError("unreachable — the timeout must fire first")

    hosted = HostedAudioTts(
        inner=SlowTts(),
        store=LocalVoiceAudioStore(),
        admin_id=ADMIN,
        instance_id=INSTANCE,
        call_sid="CA-slow",
        ttl_seconds=900,
        synthesize_timeout_seconds=0.05,
    )
    result = await hosted.synthesize("hello?")
    assert result.playable is False and result.text == "hello?"


async def test_builder_is_honest_say_when_live_speech_cannot_bind(monkeypatch, caplog):
    """Live + bucket + opt-in, but the platform secret has no ElevenLabs key:
    degraded voice quality (Twilio <Say>), never a crashed webhook."""
    monkeypatch.setenv("LUCIEL_VOICE_AUDIO_BUCKET", "luciel-dev-voice-audio-123")
    monkeypatch.setenv("LUCIEL_VOICE_PREMIUM_AUDIO", "1")
    settings = __import__("app.config", fromlist=["get_settings"]).get_settings()
    monkeypatch.setattr(settings, "integration_mode", "live")
    monkeypatch.setattr(settings, "platform_secret", "{}")

    with caplog.at_level("WARNING", logger="app.runtime.voice_audio"):
        built = build_hosted_voice_tts(admin_id=ADMIN, instance_id=INSTANCE, call_sid="CA")
    assert built is None
    assert any("voice_audio_hosting_unavailable" in r.getMessage() for r in caplog.records)


async def test_builder_hosts_when_live_with_bucket_keys_and_opt_in(monkeypatch):
    monkeypatch.setenv("LUCIEL_VOICE_AUDIO_BUCKET", "luciel-dev-voice-audio-123")
    monkeypatch.setenv("LUCIEL_VOICE_PREMIUM_AUDIO", "1")
    settings = __import__("app.config", fromlist=["get_settings"]).get_settings()
    monkeypatch.setattr(settings, "integration_mode", "live")
    monkeypatch.setattr(
        settings,
        "platform_secret",
        '{"OPENAI_API_KEY": "sk-fake", "ELEVENLABS_API_KEY": "el-fake"}',
    )
    built = build_hosted_voice_tts(admin_id=ADMIN, instance_id=INSTANCE, call_sid="CA")
    assert isinstance(built, HostedAudioTts)
    assert built.name == "hosted_audio_tts"


async def test_hosted_wrapper_satisfies_the_texttospeech_protocol():
    hosted = _hosted(EchoTextToSpeech(), LocalVoiceAudioStore())
    # The pipeline's injection seam is the TextToSpeech Protocol (name attribute +
    # async synthesize); the wrapper must slot in with no Protocol widening.
    assert hasattr(hosted, "synthesize") and callable(hosted.synthesize)
    assert voice_audio.HostedAudioTts.name == "hosted_audio_tts"
