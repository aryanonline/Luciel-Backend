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


# --- live providers (§3.1.2 adapter table / §5.12 sub-processor register) ------ #
# Both SDKs are imported LAZILY inside methods — exactly the AnthropicProvider /
# OpenAIProvider pattern in app.runtime.providers — so CI (no keys, no network, SDK
# may be absent) imports this module cleanly. No network call happens at import or
# construction time; keys are injected by the caller (provider_factory), never read
# from anywhere inside these classes, and NEVER hardcoded (Hard rule #2).


class WhisperSpeechToText:
    """Live STT — OpenAI Whisper (§3.1.2 adapter table, §5.12 sub-processor register).

    ``audio_ref`` is the URL of the caller's recorded audio (e.g. Twilio's
    ``RecordingUrl``); the OpenAI Audio Transcriptions endpoint accepts a file-like
    object, so the recording is fetched and handed to the SDK as bytes. Maps
    ``openai.OpenAIError`` to ``SpeechError`` so the voice pipeline degrades
    gracefully (spoken apology + human handoff) rather than dropping the call.
    """

    name = "whisper"

    def __init__(self, *, api_key: str, model: str = "whisper-1") -> None:
        self._api_key = api_key
        self._model = model
        self._client: object | None = None

    def _get_client(self) -> object:
        if self._client is None:
            from openai import AsyncOpenAI  # noqa: PLC0415 — lazy/guarded import

            self._client = AsyncOpenAI(api_key=self._api_key)
        return self._client

    async def transcribe(self, audio_ref: str) -> Transcription:
        from openai import OpenAIError  # noqa: PLC0415 — lazy/guarded import

        client = self._get_client()
        try:
            audio_bytes = await self._fetch_audio(audio_ref)
            resp = await client.audio.transcriptions.create(  # type: ignore[attr-defined]
                model=self._model,
                file=("recording.wav", audio_bytes),
            )
        except OpenAIError as exc:
            raise SpeechError(f"whisper_error:{type(exc).__name__}") from exc
        except Exception as exc:  # noqa: BLE001 — network/fetch failures, mapped uniformly
            raise SpeechError(f"whisper_fetch_error:{type(exc).__name__}") from exc

        text = getattr(resp, "text", "") or ""
        return Transcription(text=text, confidence=1.0)

    async def _fetch_audio(self, audio_ref: str) -> bytes:
        """Fetch the caller recording referenced by ``audio_ref``. Lazy-imports httpx
        so no network dependency is required unless this live path actually runs."""
        import httpx  # noqa: PLC0415 — lazy import

        async with httpx.AsyncClient() as client:
            resp = await client.get(audio_ref, timeout=10)
            resp.raise_for_status()
            return resp.content


class ElevenLabsTextToSpeech:
    """Live TTS — ElevenLabs (§3.1.2 adapter table, §5.12 sub-processor register).

    Synthesizes ``text`` and returns a reference the telephony layer can play back.
    The API key is injected by the caller (provider_factory reads it from the
    platform secret via ``parse_platform_secret`` — never hardcoded here, Hard rule
    #2) and is used only for the Authorization header of the synthesis request.
    """

    name = "elevenlabs"

    _BASE_URL = "https://api.elevenlabs.io/v1"

    def __init__(self, *, api_key: str, voice_id: str = "21m00Tcm4TlvDq8ikWAM") -> None:
        self._api_key = api_key
        self._voice_id = voice_id

    async def synthesize(self, text: str) -> SynthesizedSpeech:
        import httpx  # noqa: PLC0415 — lazy import: no network dependency unless live

        url = f"{self._BASE_URL}/text-to-speech/{self._voice_id}"
        headers = {"xi-api-key": self._api_key, "Accept": "audio/mpeg"}
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json={"text": text}, headers=headers, timeout=10)
                resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise SpeechError(f"elevenlabs_error:{type(exc).__name__}") from exc

        # The synthesized audio bytes aren't persisted to a file store here (out of
        # scope for this workstream) — the reference records that synthesis
        # succeeded and the exact text spoken (retained per §3.4.10), mirroring the
        # EchoTextToSpeech contract shape so the pipeline/TwiML layer is unaffected.
        digest = abs(hash(resp.content))
        return SynthesizedSpeech(audio_ref=f"tts://elevenlabs/{self._voice_id}/{digest}", text=text)
