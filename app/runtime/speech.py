"""Speech-to-text / text-to-speech provider interfaces for the Voice channel.

The Voice channel (Architecture §3.1.2, adapter table: "Twilio Voice + Whisper STT
+ ElevenLabs TTS") needs to (a) transcribe inbound caller audio and (b) synthesize
spoken responses. Following the same dependency-injection doctrine as the
LLMProvider Protocol (§3.4.3) and the HashingEmbedder (§3.2), the concrete vendors
are NOT hardwired: the runtime depends only on the ``SpeechToText`` and
``TextToSpeech`` Protocols, and a provider is injected. Real Whisper/ElevenLabs (or
any substitute) bind behind these interfaces in deployment; the deterministic
implementations below exercise the voice loop hermetically in tests, exactly as
EchoModel does for the LLM (§3.4.3) — no paid-vendor SDK call ever runs in CI.

A transcription/synthesis failure is surfaced as a typed ``SpeechError`` so the
voice pipeline can degrade gracefully (spoken apology + human handoff) rather than
dropping the call silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class SpeechError(Exception):
    """STT/TTS provider failure — handled by the voice pipeline, never silently dropped."""


@dataclass
class Transcription:
    text: str
    # Provider confidence in the transcript [0,1]; the pipeline may treat a very low
    # confidence as "did not understand" and reprompt (kept simple here).
    confidence: float = 1.0


@dataclass
class SynthesizedSpeech:
    # A reference the telephony layer can play back. In production this is an audio
    # URL/stream handle from the TTS vendor; for TwiML <Play> it is a URL, and a
    # <Say> fallback uses the text directly. The deterministic impl returns a stable
    # synthetic reference so tests can assert on it without audio bytes.
    audio_ref: str
    # The exact text that was synthesized — retained so TwiML can fall back to <Say>
    # and so the transcript persists the spoken wording (§3.4.10 retention).
    text: str


class SpeechToText(Protocol):
    name: str

    async def transcribe(self, audio_ref: str) -> Transcription:
        """Transcribe caller audio (referenced by a provider handle) → text.

        Raises SpeechError on provider failure.
        """
        ...


class TextToSpeech(Protocol):
    name: str

    async def synthesize(self, text: str) -> SynthesizedSpeech:
        """Synthesize spoken audio for ``text``. Raises SpeechError on failure."""
        ...


class EchoSpeechToText:
    """Deterministic STT for the voice loop + tests (analogous to EchoModel).

    Twilio's inbound voice webhook already supplies a server-side transcript of the
    caller's speech (the ``SpeechResult`` param from <Gather input="speech">); the
    deterministic STT models that by treating the provided audio_ref AS the
    transcript, so the voice loop is fully exercisable without any audio pipeline.
    """

    name = "echo_stt"

    def __init__(self, *, raise_error: bool = False, confidence: float = 1.0) -> None:
        self._raise = raise_error
        self._confidence = confidence

    async def transcribe(self, audio_ref: str) -> Transcription:
        if self._raise:
            raise SpeechError(f"{self.name}_unavailable")
        return Transcription(text=audio_ref, confidence=self._confidence)


class EchoTextToSpeech:
    """Deterministic TTS for the voice loop + tests (analogous to EchoModel).

    Returns a stable synthetic audio reference derived from the text so tests can
    assert the spoken content round-trips, and so TwiML generation can fall back to
    <Say> with the same text.
    """

    name = "echo_tts"

    def __init__(self, *, raise_error: bool = False) -> None:
        self._raise = raise_error

    async def synthesize(self, text: str) -> SynthesizedSpeech:
        if self._raise:
            raise SpeechError(f"{self.name}_unavailable")
        return SynthesizedSpeech(audio_ref=f"tts://echo/{abs(hash(text))}", text=text)
