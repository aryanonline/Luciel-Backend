"""Live voice STT/TTS providers + mode-aware speech factory (§3.1.2 / §5.12).

WhisperSpeechToText (OpenAI) and ElevenLabsTextToSpeech are new live-wireable
providers alongside the existing Echo doubles. Every test here is hermetic: SDK
clients and HTTP calls are patched with ``unittest.mock`` so NO real network call
ever happens, and the ElevenLabs key is only ever a fake string constructed in the
test body — never read from Credentials.docx or hardcoded anywhere in source.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.config import Settings
from app.runtime.provider_factory import build_speech_providers
from app.runtime.speech import (
    EchoSpeechToText,
    EchoTextToSpeech,
    ElevenLabsTextToSpeech,
    SpeechError,
    SynthesizedSpeech,
    Transcription,
    WhisperSpeechToText,
)

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
    # The API key never appears in the returned reference.
    assert "fake-elevenlabs-key" not in out.audio_ref
    fake_client.post.assert_awaited_once()
    _, kwargs = fake_client.post.call_args
    assert kwargs["headers"]["xi-api-key"] == "fake-elevenlabs-key"


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


# --- mode-aware speech provider factory (mirrors build_llm_router) ---------- #
def test_mock_mode_builds_echo_speech_providers():
    settings = Settings(integration_mode="mock")
    stt, tts = build_speech_providers(settings)
    assert isinstance(stt, EchoSpeechToText)
    assert isinstance(tts, EchoTextToSpeech)


def test_default_mode_is_mock_for_speech_too():
    settings = Settings()
    assert settings.integration_mode == "mock"
    stt, tts = build_speech_providers(settings)
    assert isinstance(stt, EchoSpeechToText)


def test_live_mode_builds_whisper_and_elevenlabs_from_secret():
    secret = json.dumps(
        {
            "ANTHROPIC_API_KEY": "fake-anthropic-key",
            "OPENAI_API_KEY": "fake-openai-key",
            "ELEVENLABS_API_KEY": "fake-elevenlabs-key",
        }
    )
    settings = Settings(integration_mode="live", platform_secret=secret)
    stt, tts = build_speech_providers(settings)
    assert isinstance(stt, WhisperSpeechToText)
    assert isinstance(tts, ElevenLabsTextToSpeech)
    assert stt._api_key == "fake-openai-key"
    assert tts._api_key == "fake-elevenlabs-key"


def test_live_mode_missing_elevenlabs_key_raises_without_echoing_value():
    secret = json.dumps({"OPENAI_API_KEY": "fake-openai-key"})
    settings = Settings(integration_mode="live", platform_secret=secret)
    with pytest.raises(RuntimeError) as exc:
        build_speech_providers(settings)
    assert "ELEVENLABS_API_KEY" in str(exc.value)
    assert "fake-openai-key" not in str(exc.value)


def test_live_mode_missing_openai_key_raises():
    secret = json.dumps({"ELEVENLABS_API_KEY": "fake-elevenlabs-key"})
    settings = Settings(integration_mode="live", platform_secret=secret)
    with pytest.raises(RuntimeError) as exc:
        build_speech_providers(settings)
    assert "OPENAI_API_KEY" in str(exc.value)
