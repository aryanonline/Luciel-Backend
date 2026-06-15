"""STT/TTS Protocol round-trip with the deterministic test impls (Architecture §3.1.2).

The Voice channel depends only on the SpeechToText / TextToSpeech Protocols and an
injected provider (same DI doctrine as EchoModel §3.4.3); these tests exercise the
deterministic Echo impls and the typed SpeechError degrade path — no paid-vendor SDK.
"""

from __future__ import annotations

import pytest
from app.runtime.speech import (
    EchoSpeechToText,
    EchoTextToSpeech,
    SpeechError,
    SynthesizedSpeech,
    Transcription,
)

pytestmark = [pytest.mark.asyncio]


async def test_echo_stt_treats_audio_ref_as_transcript():
    # Twilio supplies a server-side transcript (SpeechResult); the Echo STT models that
    # by returning the audio_ref AS the transcript so the loop is exercisable hermetically.
    stt = EchoSpeechToText()
    out = await stt.transcribe("are you open on saturday?")
    assert isinstance(out, Transcription)
    assert out.text == "are you open on saturday?"
    assert out.confidence == 1.0


async def test_echo_stt_raises_speech_error_when_configured():
    stt = EchoSpeechToText(raise_error=True)
    with pytest.raises(SpeechError):
        await stt.transcribe("anything")


async def test_echo_tts_round_trips_text_and_is_deterministic():
    tts = EchoTextToSpeech()
    a = await tts.synthesize("We open at nine.")
    b = await tts.synthesize("We open at nine.")
    assert isinstance(a, SynthesizedSpeech)
    assert a.text == "We open at nine."
    assert a.audio_ref.startswith("tts://echo/")
    # Same text → same synthetic reference (deterministic, assertable without audio bytes).
    assert a.audio_ref == b.audio_ref


async def test_echo_tts_raises_speech_error_when_configured():
    tts = EchoTextToSpeech(raise_error=True)
    with pytest.raises(SpeechError):
        await tts.synthesize("anything")
