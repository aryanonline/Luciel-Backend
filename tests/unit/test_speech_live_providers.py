"""Live voice STT/TTS providers + mode-aware TTS factory (§3.1.2 / §5.12).

WhisperSpeechToText (OpenAI) and ElevenLabsTextToSpeech are live-wireable providers
alongside the Echo doubles. Every test here is hermetic: SDK clients and HTTP calls
are patched with ``unittest.mock`` so NO real network call ever happens, and the
ElevenLabs key is only ever a fake string constructed in the test body — never read
from Credentials.docx or hardcoded anywhere in source.

Round 7 WP-6 item 9: the factory builds TTS only (``build_text_to_speech``) — live STT
is Twilio's own speech transcript, so the Whisper construction was unreachable and is
gone; the class stays (these tests import it). The ElevenLabs voice and model ids come
from Settings (platform-secret rotatable), and the model id is sent on every request.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import Settings
from app.runtime.provider_factory import build_text_to_speech
from app.runtime.speech import (
    EchoTextToSpeech,
    ElevenLabsTextToSpeech,
    SpeechError,
    SynthesizedSpeech,
    Transcription,
    WhisperSpeechToText,
)

from tests.unit.live_settings import live_settings

pytestmark = [pytest.mark.asyncio]


# --- WhisperSpeechToText ---------------------------------------------------- #
async def test_whisper_stt_transcribes_via_mocked_openai_client():
    stt = WhisperSpeechToText(api_key="fake-openai-key")
    fake_resp = MagicMock()
    fake_resp.text = "are you open on saturday"

    fake_client = MagicMock()
    fake_client.audio.transcriptions.create = AsyncMock(return_value=fake_resp)

    with (
        patch.object(WhisperSpeechToText, "_get_client", return_value=fake_client),
        patch.object(
            WhisperSpeechToText, "_fetch_audio", new=AsyncMock(return_value=b"fake-bytes")
        ),
    ):
        out = await stt.transcribe("https://example.com/recording.wav")

    assert isinstance(out, Transcription)
    assert out.text == "are you open on saturday"
    fake_client.audio.transcriptions.create.assert_awaited_once()


async def test_whisper_stt_maps_openai_error_to_speech_error():
    from openai import OpenAIError

    stt = WhisperSpeechToText(api_key="fake-openai-key")
    fake_client = MagicMock()
    fake_client.audio.transcriptions.create = AsyncMock(side_effect=OpenAIError("boom"))

    with (
        patch.object(WhisperSpeechToText, "_get_client", return_value=fake_client),
        patch.object(
            WhisperSpeechToText, "_fetch_audio", new=AsyncMock(return_value=b"fake-bytes")
        ),
    ):
        with pytest.raises(SpeechError):
            await stt.transcribe("https://example.com/recording.wav")


async def test_whisper_stt_maps_fetch_failure_to_speech_error():
    stt = WhisperSpeechToText(api_key="fake-openai-key")
    with patch.object(
        WhisperSpeechToText, "_fetch_audio", new=AsyncMock(side_effect=RuntimeError("network down"))
    ):
        with pytest.raises(SpeechError):
            await stt.transcribe("https://example.com/recording.wav")


async def test_whisper_stt_never_hardcodes_key_uses_injected_value():
    stt = WhisperSpeechToText(api_key="fake-only-in-this-test")
    assert stt._api_key == "fake-only-in-this-test"
    assert stt.name == "whisper"


# --- ElevenLabsTextToSpeech -------------------------------------------------- #
async def test_elevenlabs_tts_synthesizes_via_mocked_httpx():
    tts = ElevenLabsTextToSpeech(api_key="fake-elevenlabs-key")
    fake_resp = MagicMock()
    fake_resp.content = b"fake-mp3-bytes"
    fake_resp.raise_for_status = MagicMock()

    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=fake_resp)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=fake_client):
        out = await tts.synthesize("We open at nine.")

    assert isinstance(out, SynthesizedSpeech)
    assert out.text == "We open at nine."
    assert out.audio_ref.startswith("tts://elevenlabs/")
    # The mp3 bytes ride along for the hosting layer (voice_audio.HostedAudioTts)
    # instead of being discarded — without them there is nothing to <Play>.
    assert out.audio_bytes == b"fake-mp3-bytes"
    assert out.playable is False  # only the hosting layer may mark a result playable
    # The API key never appears in the returned reference.
    assert "fake-elevenlabs-key" not in out.audio_ref
    fake_client.post.assert_awaited_once()
    args, kwargs = fake_client.post.call_args
    assert kwargs["headers"]["xi-api-key"] == "fake-elevenlabs-key"
    # Round 7 WP-6 item 9: the voice and the model are the platform settings, and the
    # model id is SENT — without it ElevenLabs picks (and can change) its default.
    settings = Settings()
    assert args[0].endswith(f"/text-to-speech/{settings.elevenlabs_voice_id}")
    assert kwargs["json"] == {"text": "We open at nine.", "model_id": settings.elevenlabs_model_id}


async def test_elevenlabs_voice_and_model_are_injected_not_literal():
    tts = ElevenLabsTextToSpeech(api_key="k", voice_id="voice-xyz", model_id="eleven_flash_v2_5")
    fake_resp = MagicMock()
    fake_resp.content = b"mp3"
    fake_resp.raise_for_status = MagicMock()
    fake_client = AsyncMock()
    fake_client.post = AsyncMock(return_value=fake_resp)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)
    with patch("httpx.AsyncClient", return_value=fake_client):
        out = await tts.synthesize("hi")
    args, kwargs = fake_client.post.call_args
    assert args[0] == "https://api.elevenlabs.io/v1/text-to-speech/voice-xyz"
    assert kwargs["json"]["model_id"] == "eleven_flash_v2_5"
    assert out.audio_ref.startswith("tts://elevenlabs/voice-xyz/")


def test_no_vendor_id_literal_is_left_in_the_speech_module():
    """The rotatable ids live in Settings only: a literal left behind in speech.py would
    silently outlive a rotation through the platform secret."""
    import inspect

    from app.runtime import speech

    source = inspect.getsource(speech.ElevenLabsTextToSpeech)
    assert Settings().elevenlabs_voice_id not in source
    assert Settings().elevenlabs_model_id not in source


async def test_elevenlabs_tts_maps_http_error_to_speech_error():
    import httpx

    tts = ElevenLabsTextToSpeech(api_key="fake-elevenlabs-key")
    fake_client = AsyncMock()
    fake_client.post = AsyncMock(side_effect=httpx.HTTPError("boom"))
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=False)

    with patch("httpx.AsyncClient", return_value=fake_client):
        with pytest.raises(SpeechError):
            await tts.synthesize("anything")


async def test_elevenlabs_never_hardcodes_key_uses_injected_value():
    tts = ElevenLabsTextToSpeech(api_key="fake-only-in-this-test")
    assert tts._api_key == "fake-only-in-this-test"
    assert tts.name == "elevenlabs"


# --- mode-aware TTS factory (mirrors build_llm_router) ---------------------- #
def test_mock_mode_builds_the_echo_tts():
    assert isinstance(build_text_to_speech(Settings(integration_mode="mock")), EchoTextToSpeech)


def test_default_mode_is_mock_for_speech_too():
    settings = Settings()
    assert settings.integration_mode == "mock"
    assert isinstance(build_text_to_speech(settings), EchoTextToSpeech)


def test_live_mode_builds_elevenlabs_from_secret_with_the_pinned_ids():
    settings = live_settings(
        ELEVENLABS_API_KEY="fake-elevenlabs-key",
        ELEVENLABS_VOICE_ID="voice-from-secret",
        ELEVENLABS_MODEL_ID="model-from-secret",
    )
    tts = build_text_to_speech(settings)
    assert isinstance(tts, ElevenLabsTextToSpeech)
    assert tts._api_key == "fake-elevenlabs-key"
    assert (tts._voice_id, tts._model_id) == ("voice-from-secret", "model-from-secret")


def test_live_tts_no_longer_needs_the_openai_key():
    """The Whisper STT that needed OPENAI_API_KEY was never reachable (live STT is
    Twilio's own transcript); the TTS factory needs ElevenLabs only."""
    tts = build_text_to_speech(live_settings(ELEVENLABS_API_KEY="fake-elevenlabs-key"))
    assert isinstance(tts, ElevenLabsTextToSpeech)


def test_live_mode_missing_elevenlabs_key_raises_without_echoing_value():
    settings = live_settings(OPENAI_API_KEY="fake-openai-key")
    with pytest.raises(RuntimeError) as exc:
        build_text_to_speech(settings)
    assert "ELEVENLABS_API_KEY" in str(exc.value)
    assert "fake-openai-key" not in str(exc.value)


def test_elevenlabs_ids_bridge_env_over_secret_over_default(monkeypatch):
    assert live_settings().elevenlabs_voice_id == Settings().elevenlabs_voice_id
    assert live_settings(ELEVENLABS_MODEL_ID="m-blob").elevenlabs_model_id == "m-blob"
    monkeypatch.setenv("LUCIEL_ELEVENLABS_VOICE_ID", "v-env")
    assert live_settings(ELEVENLABS_VOICE_ID="v-blob").elevenlabs_voice_id == "v-env"
