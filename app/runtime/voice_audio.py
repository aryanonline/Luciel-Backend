"""Voice audio hosting — the ``<Play>`` surface ElevenLabs was waiting for (§3.1.2).

Voice replies were delivered by Twilio's built-in ``<Say>`` voice because the
platform had nowhere to HOST synthesized audio: Twilio can only play a fetchable
``audio/mpeg`` URL, and ``ElevenLabsTextToSpeech`` had the mp3 bytes in hand and
threw them away. This module is that hosting layer:

  * :class:`HostedAudioTts` — a ``TextToSpeech`` wrapper that synthesizes with the
    inner provider, uploads the bytes to S3 under the tenant prefix, and returns a
    short-lived presigned URL the TwiML layer renders as ``<Play>``. It NEVER
    raises: any failure (provider, upload, presign) degrades to a non-playable
    result whose text the TwiML layer speaks via ``<Say>`` — a worse voice must
    never become a dead call.
  * :class:`S3VoiceAudioStore` / :class:`LocalVoiceAudioStore` — the storage seam,
    mirroring ``export_store`` (same presign client config) with the
    ``transcript_store`` BINDING GATE: the real store binds on the CDK-injected
    ``LUCIEL_VOICE_AUDIO_BUCKET`` env var, not on ``integration_mode`` — S3 is our
    own storage, not a third party.
  * :func:`build_hosted_voice_tts` — the per-request delivery builder the voice
    webhook calls. It additionally gates on live mode, because the INNER vendor
    (ElevenLabs) IS a paid third party: mock mode keeps the honest ``<Say>``
    delivery and never constructs a synthesis client.

Deliberately NOT in the delete cascade: objects here are single-call artifacts
behind a ≤15-minute link, and the bucket's 1-day lifecycle rule deletes them
regardless of tenant state — a deleted tenant's audio is gone within ~24-48h by
construction, vs the ~1-year exposure that forced transcripts into the cascade.

The opening disclosure stays ``<Say>`` (conscious scope): ``start_call`` performs
no synthesis today, and the compliance notice does not need the premium voice.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from typing import Protocol, runtime_checkable

from app.runtime.speech import SpeechError, SynthesizedSpeech, TextToSpeech

logger = logging.getLogger(__name__)


def voice_audio_key_for(admin_id: uuid.UUID, instance_id: uuid.UUID, call_sid: str) -> str:
    """Per-tenant object key (the ``vm/{admin_id}/`` prefix doctrine, §3.8.3)."""
    call_part = call_sid or "unknown"
    return f"vm/{admin_id}/voice/{instance_id}/{call_part}/{uuid.uuid4()}.mp3"


@runtime_checkable
class VoiceAudioStore(Protocol):
    """Persist one turn's audio and mint a short-lived playback URL."""

    async def put_and_presign(self, *, key: str, data: bytes, ttl_seconds: int) -> str:
        """Upload ``data`` under ``key``; return a GET URL valid ``ttl_seconds``."""
        ...


class LocalVoiceAudioStore:
    """In-process store for dev/test (mirrors LocalExportStore).

    The fake URL deliberately carries TWO query params so any TwiML rendering of
    it must escape the ``&`` separator — the same property a real presigned URL
    has, exercised without AWS."""

    _store: dict[str, bytes] = {}
    _lock = threading.Lock()

    async def put_and_presign(self, *, key: str, data: bytes, ttl_seconds: int) -> str:
        with self._lock:
            type(self)._store[key] = data
        expires_at = int(time.time()) + ttl_seconds
        return f"https://voice-audio.local/{key}?X-Amz-Expires={ttl_seconds}&expires={expires_at}"

    @classmethod
    def get_bytes(cls, key: str) -> bytes | None:
        """Test hook: read back the persisted audio bytes."""
        with cls._lock:
            return cls._store.get(key)

    @classmethod
    def reset(cls) -> None:
        """Test hook: clear the process-global store between tests."""
        with cls._lock:
            cls._store.clear()


class S3VoiceAudioStore:
    """Real voice-audio S3 backend. boto3 is imported lazily (CI has no AWS).

    The blocking put+presign runs in a worker thread (``asyncio.to_thread``):
    this sits on the live-call webhook path, racing Twilio's ~15s TwiML fetch
    window, and a sync S3 call on the event loop would also stall every other
    in-flight request on the worker."""

    def __init__(self, *, bucket: str, region_name: str) -> None:
        self._bucket = bucket
        self._region_name = region_name
        self._client: object | None = None

    def _get_client(self) -> object:
        if self._client is None:
            import boto3  # noqa: PLC0415 — lazy/guarded import (CI has no AWS)
            from botocore.config import Config  # noqa: PLC0415

            # Pin the REGIONAL endpoint + SigV4 virtual-host addressing. Without
            # this, boto3's default (global) endpoint mints presigned URLs that
            # fail SigV4 verification for a regional bucket (403 SignatureDoesNot-
            # Match on the GET). Same config as S3ExportStore, verified against
            # the live ca-central-1 export bucket.
            self._client = boto3.client(
                "s3",
                region_name=self._region_name,
                endpoint_url=f"https://s3.{self._region_name}.amazonaws.com",
                config=Config(
                    signature_version="s3v4",
                    s3={"addressing_style": "virtual"},
                ),
            )
        return self._client

    def _put_and_presign_sync(self, *, key: str, data: bytes, ttl_seconds: int) -> str:
        client = self._get_client()
        client.put_object(  # type: ignore[attr-defined]
            Bucket=self._bucket,
            Key=key,
            Body=data,
            # Twilio <Play> requires a fetchable audio/mpeg.
            ContentType="audio/mpeg",
            ServerSideEncryption="aws:kms",
        )
        url = client.generate_presigned_url(  # type: ignore[attr-defined]
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=ttl_seconds,
        )
        return str(url)

    async def put_and_presign(self, *, key: str, data: bytes, ttl_seconds: int) -> str:
        return await asyncio.to_thread(
            self._put_and_presign_sync, key=key, data=data, ttl_seconds=ttl_seconds
        )


_default: VoiceAudioStore | None = None


def get_voice_audio_store() -> VoiceAudioStore:
    """The bound VoiceAudioStore — real iff the infrastructure gave us a bucket.

    Keyed on the CDK-injected ``LUCIEL_VOICE_AUDIO_BUCKET`` env var, NOT on
    ``integration_mode``, for the same reason ``get_transcript_store`` is: S3 is
    our own storage, and the settings field carries a placeholder default that
    cannot distinguish configured from defaulted. Local runs and CI set no such
    variable and keep the in-process fake, so the suite stays hermetic."""
    global _default
    if _default is None:
        import os  # noqa: PLC0415 — deployment shape, read at bind time

        from app.config import get_settings  # noqa: PLC0415 — avoid import cycle

        settings = get_settings()
        if os.environ.get("LUCIEL_VOICE_AUDIO_BUCKET"):
            _default = S3VoiceAudioStore(
                bucket=settings.voice_audio_bucket, region_name=settings.aws_region
            )
        else:
            _default = LocalVoiceAudioStore()
    return _default


def set_voice_audio_store(store: VoiceAudioStore | None) -> None:
    """Override the store (tests inject a fresh fake; None resets to default)."""
    global _default
    _default = store


class HostedAudioTts:
    """TextToSpeech wrapper: synthesize with the inner provider, host the bytes,
    answer with a playable presigned URL. Never raises — every failure path
    returns a NON-playable result whose text the TwiML layer ``<Say>``s, because
    a degraded voice must never become a dead call (§3.1.2)."""

    name = "hosted_audio_tts"

    def __init__(
        self,
        *,
        inner: TextToSpeech,
        store: VoiceAudioStore,
        admin_id: uuid.UUID,
        instance_id: uuid.UUID,
        call_sid: str,
        ttl_seconds: int,
        synthesize_timeout_seconds: float = 6.0,
    ) -> None:
        self._inner = inner
        self._store = store
        self._admin_id = admin_id
        self._instance_id = instance_id
        self._call_sid = call_sid
        self._ttl_seconds = ttl_seconds
        # Twilio abandons a webhook after ~15s; the reply TwiML also carries the
        # LLM turn's latency, so a slow synthesis provider must be cut off early
        # enough that the degraded <Say> answer still makes the window (the c11
        # worst-case-latency risk, promoted to a hard bound 2026-08-18).
        self._synthesize_timeout = synthesize_timeout_seconds

    async def synthesize(self, text: str) -> SynthesizedSpeech:
        inner_name = getattr(self._inner, "name", type(self._inner).__name__)
        try:
            speech = await asyncio.wait_for(
                self._inner.synthesize(text), timeout=self._synthesize_timeout
            )
        except TimeoutError:
            logger.warning(
                "voice_audio_hosting_failed stage=synthesize_timeout provider=%s after=%ss",
                inner_name,
                self._synthesize_timeout,
            )
            return SynthesizedSpeech(audio_ref=f"say-fallback:{len(text)}", text=text)
        except SpeechError as exc:
            logger.warning(
                "voice_audio_hosting_failed stage=synthesize provider=%s error=%s",
                inner_name,
                exc,
            )
            return SynthesizedSpeech(audio_ref=f"say-fallback:{len(text)}", text=text)
        if not speech.audio_bytes:
            # A provider that returned no bytes (Echo without the field, an older
            # impl) cannot be hosted — say the text rather than play nothing.
            logger.warning("voice_audio_hosting_failed stage=no_bytes provider=%s", inner_name)
            return SynthesizedSpeech(audio_ref=speech.audio_ref, text=text)
        try:
            url = await self._store.put_and_presign(
                key=voice_audio_key_for(self._admin_id, self._instance_id, self._call_sid),
                data=speech.audio_bytes,
                ttl_seconds=self._ttl_seconds,
            )
        except Exception as exc:  # noqa: BLE001 — ANY hosting failure degrades to <Say>
            logger.warning("voice_audio_hosting_failed stage=store error=%s", type(exc).__name__)
            return SynthesizedSpeech(audio_ref=speech.audio_ref, text=text)
        return SynthesizedSpeech(audio_ref=url, text=text, playable=True)


def build_hosted_voice_tts(
    *, admin_id: uuid.UUID, instance_id: uuid.UUID, call_sid: str
) -> TextToSpeech | None:
    """The per-request delivery builder the voice webhook calls.

    Returns None — meaning "use the honest ``<Say>`` delivery" — unless ALL THREE
    gates pass: live mode (the inner ElevenLabs synthesis is a paid third-party
    call, so it obeys the same mode gate as every vendor), a real bucket
    (``LUCIEL_VOICE_AUDIO_BUCKET`` injected by CDK — a Local store's fake URL
    must never be rendered into real TwiML), AND the explicit premium-audio
    opt-in ``LUCIEL_VOICE_PREMIUM_AUDIO=1``. The opt-in exists because the CDK
    wires the bucket unconditionally, so bucket-presence alone re-enabled the
    hosting layer the owner deliberately deferred (2026-08-17 "ship with <Say>")
    — live-caught as a failing paid ElevenLabs round-trip on EVERY reply against
    the unfunded account. A live deployment whose speech providers fail to build
    (missing key) also answers None, logged: degraded voice quality, never a
    dead call."""
    import os  # noqa: PLC0415 — deployment shape, read per request

    from app.config import get_settings  # noqa: PLC0415 — avoid import cycle

    settings = get_settings()
    if (
        settings.integration_mode != "live"
        or not os.environ.get("LUCIEL_VOICE_AUDIO_BUCKET")
        or os.environ.get("LUCIEL_VOICE_PREMIUM_AUDIO") != "1"
    ):
        return None
    try:
        from app.runtime.provider_factory import build_text_to_speech  # noqa: PLC0415

        tts = build_text_to_speech(settings)
    except RuntimeError as exc:
        # Missing platform key: the honest degraded state is Twilio's <Say> voice.
        logger.warning("voice_audio_hosting_unavailable reason=%s", exc)
        return None
    return HostedAudioTts(
        inner=tts,
        store=get_voice_audio_store(),
        admin_id=admin_id,
        instance_id=instance_id,
        call_sid=call_sid,
        ttl_seconds=settings.voice_audio_link_ttl_seconds,
    )
