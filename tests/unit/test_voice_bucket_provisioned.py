"""The voice-audio bucket the app presigns from has to exist, expire, and be granted.

Same wiring-assertion shape as ``test_export_bucket_provisioned.py`` (and the P1-14
lesson behind it): ``S3VoiceAudioStore`` addresses the bucket by an injected name, so
neither side fails alone — an unprovisioned bucket is an AccessDenied at the first
live ``<Play>`` and a silent ``<Say>`` fallback forever after. The test reads both
halves.
"""

from __future__ import annotations

import re
from pathlib import Path

_INFRA = Path(__file__).resolve().parents[2] / "infra" / "lib"
_DATA_STACK = (_INFRA / "data-stack.ts").read_text(encoding="utf-8")
_COMPUTE_STACK = (_INFRA / "compute-stack.ts").read_text(encoding="utf-8")


def _voice_bucket_block() -> str:
    block = re.search(
        r"this\.voiceAudioBucket = residency\.hardenedBucket\((.*?)\n    \}\);",
        _DATA_STACK,
        re.DOTALL,
    )
    assert block, "the voice-audio bucket is not provisioned in data-stack.ts"
    return block.group(1)


def test_the_voice_audio_bucket_expires_its_objects_within_a_day():
    """Objects are single-call artifacts behind a ≤15-minute link. The 1-day expiry
    is both the storage plateau and the deleted-tenant bound (the documented reason
    these objects are NOT in the delete cascade)."""
    days = re.search(r"expiration: Duration\.days\((\d+)\)", _voice_bucket_block())
    assert days, "the voice-audio bucket has no expiration rule"
    assert int(days.group(1)) == 1
    assert "abortIncompleteMultipartUploadAfter" in _voice_bucket_block()


def test_the_task_role_can_write_and_the_presigner_can_decrypt():
    # Shared app-plane grant (F115) for the control plane + sweeps; the data plane
    # holds its own explicit grant on the same bucket.
    assert "voiceAudioBucket.grantReadWrite(role)" in _COMPUTE_STACK
    assert "voiceAudioBucket.grantReadWrite(dataPlaneRole)" in _COMPUTE_STACK
    # The presigned GET is authorized AS the signing role and the object is
    # CMK-encrypted, so the role's kms:Decrypt (UseCmk) is load-bearing for
    # Twilio's fetch — losing it breaks <Play> invisibly.
    assert "sid: 'UseCmk'" in _COMPUTE_STACK
    assert "'kms:Decrypt'" in _COMPUTE_STACK


def test_the_bucket_name_is_injected_rather_than_hardcoded_in_the_app():
    assert "LUCIEL_VOICE_AUDIO_BUCKET: voiceAudioBucket.bucketName" in _COMPUTE_STACK
    assert "luciel-${envConfig.envName}-voice-audio" in _voice_bucket_block()
