"""TranscriptStore — swappable persistence for cold transcript archives (Commitment
#38, Architecture §3.4.10 / §4.4).

The ``transcript_archive`` sweep serializes a session's transcript rows to JSONL and
moves them here at the 90-day hot boundary; the dashboard conversation view and the
account export read them back on demand; the retention sweep and the hard-delete
cascade delete them. This module mirrors the ``export_store.py`` seam exactly (same
shape, same mock/live gate) so the archive path follows the same AWS-DEFERRED SEAM
discipline as the rest of the codebase:

  * :class:`LocalTranscriptStore` — in-process, deterministic fake used in every test
    and in the default ``mock`` integration mode. NO network call.
  * :class:`S3TranscriptStore` — the real transcript-archive S3 backend (boto3),
    imported LAZILY so CI (no AWS) imports this module cleanly. Objects are written
    ``STANDARD_IA`` — they arrive already 90 days cold, so there is no warm phase to
    tier through — with SSE-KMS. Bound ONLY when ``integration_mode == "live"``.

Key convention: ``vm/{admin_id}/transcripts/{instance_id}/{session_id}.jsonl`` — the
``vm/{admin_id}/transcripts/`` tenant prefix is what lets the hard-delete cascade
remove every archived object for a tenant in one prefix sweep (mirroring the
Secrets Manager ``vm/{admin_id}/`` prefix cleanup, §3.8.3).
"""

from __future__ import annotations

import threading
import uuid
from typing import Protocol, runtime_checkable


def transcript_prefix_for(admin_id: uuid.UUID) -> str:
    """The tenant's archive prefix — the cascade's one-sweep deletion handle."""
    return f"vm/{admin_id}/transcripts/"


def transcript_key_for(admin_id: uuid.UUID, instance_id: uuid.UUID, session_id: uuid.UUID) -> str:
    return f"{transcript_prefix_for(admin_id)}{instance_id}/{session_id}.jsonl"


@runtime_checkable
class TranscriptStore(Protocol):
    """Persist / read / delete archived-transcript JSONL objects (Commitment #38)."""

    #: Whether this store OUTLIVES the process that wrote to it. The archive sweep
    #: DELETES the hot Postgres rows once it has written the object, so a store that
    #: does not survive its own process would destroy the transcript rather than move
    #: it. Only a real remote store may set this True.
    durable: bool

    async def put(self, *, key: str, data: bytes) -> None:
        """Upload ``data`` under ``key``. Overwrite-idempotent: re-archiving the same
        session writes the same key, so an at-least-once sweep cannot duplicate."""
        ...

    async def get(self, *, key: str) -> bytes | None:
        """Read the object back, or None if absent."""
        ...

    async def delete(self, *, key: str) -> None:
        """Delete one object (idempotent — deleting an absent key is a no-op)."""
        ...

    async def delete_prefix(self, *, prefix: str) -> int:
        """Delete every object under ``prefix``; return how many were deleted."""
        ...


class LocalTranscriptStore:
    """In-process, deterministic TranscriptStore for dev/test (mirrors LocalExportStore).

    NOT durable, and that is load-bearing: the dict dies with the process. The archive
    sweep reads :attr:`durable` and refuses to move (and therefore delete) transcripts
    into a store that cannot outlive it — see ``transcript_archive.archive_aged_transcripts``.
    Found on dev: a mock-mode sweep ran in its own process, deleted the hot rows, wrote
    the object into that process's memory, and exited — the conversation was gone and
    the session pointed at a key that resolved to nothing.
    """

    durable = False

    _store: dict[str, bytes] = {}
    _lock = threading.Lock()

    async def put(self, *, key: str, data: bytes) -> None:
        with self._lock:
            type(self)._store[key] = data

    async def get(self, *, key: str) -> bytes | None:
        with self._lock:
            return type(self)._store.get(key)

    async def delete(self, *, key: str) -> None:
        with self._lock:
            type(self)._store.pop(key, None)

    async def delete_prefix(self, *, prefix: str) -> int:
        with self._lock:
            doomed = [k for k in type(self)._store if k.startswith(prefix)]
            for k in doomed:
                del type(self)._store[k]
            return len(doomed)

    @classmethod
    def keys(cls) -> list[str]:
        """Test hook: the stored keys, for placement/isolation assertions."""
        with cls._lock:
            return sorted(cls._store)

    @classmethod
    def reset(cls) -> None:
        """Test hook: clear the process-global store between tests."""
        with cls._lock:
            cls._store.clear()


class S3TranscriptStore:
    """Real transcript-archive S3 backend (Commitment #38).

    Objects are written STANDARD_IA (they arrive already 90 days cold) with SSE-KMS.
    The bucket's own 365-days-from-upload expiration is only the §4.4 backstop; the
    retention sweep's :meth:`delete` at the 1-year transcript-age boundary is the
    authoritative deletion (see the CDK anchoring note in data-stack.ts).

    boto3 is imported LAZILY so CI (no AWS) still imports this module. Only selected
    in ``live`` mode by :func:`get_transcript_store`; never the default."""

    #: S3 outlives every process that writes to it — the one store the archive sweep
    #: may delete hot rows into.
    durable = True

    def __init__(self, *, bucket: str, region_name: str) -> None:
        self._bucket = bucket
        self._region_name = region_name
        self._client: object | None = None

    def _get_client(self) -> object:
        if self._client is None:
            import boto3  # noqa: PLC0415 — lazy/guarded import (CI has no AWS)

            self._client = boto3.client("s3", region_name=self._region_name)
        return self._client

    async def put(self, *, key: str, data: bytes) -> None:
        self._get_client().put_object(  # type: ignore[attr-defined]
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType="application/jsonl",
            ServerSideEncryption="aws:kms",
            StorageClass="STANDARD_IA",
        )

    async def get(self, *, key: str) -> bytes | None:
        client = self._get_client()
        try:
            resp = client.get_object(Bucket=self._bucket, Key=key)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — NoSuchKey and transport errors both read as absent
            return None
        body = resp["Body"].read()
        return bytes(body)

    async def delete(self, *, key: str) -> None:
        self._get_client().delete_object(Bucket=self._bucket, Key=key)  # type: ignore[attr-defined]

    async def delete_prefix(self, *, prefix: str) -> int:
        client = self._get_client()
        deleted = 0
        token: str | None = None
        while True:
            kwargs: dict[str, object] = {"Bucket": self._bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = client.list_objects_v2(**kwargs)  # type: ignore[attr-defined]
            keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
            if keys:
                client.delete_objects(  # type: ignore[attr-defined]
                    Bucket=self._bucket, Delete={"Objects": keys, "Quiet": True}
                )
                deleted += len(keys)
            if not page.get("IsTruncated"):
                return deleted
            token = page.get("NextContinuationToken")


_default: TranscriptStore | None = None


def get_transcript_store() -> TranscriptStore:
    """Return the bound TranscriptStore (mirrors ``get_export_store``): the AWS
    backend in ``live`` integration mode, the local fake everywhere else."""
    global _default
    if _default is None:
        from app.config import get_settings  # noqa: PLC0415 — avoid import cycle

        settings = get_settings()
        if settings.integration_mode == "live":
            _default = S3TranscriptStore(
                bucket=settings.transcript_archive_bucket, region_name=settings.aws_region
            )
        else:
            _default = LocalTranscriptStore()
    return _default


def set_transcript_store(store: TranscriptStore | None) -> None:
    """Override the store (tests inject a fresh fake; None resets to default)."""
    global _default
    _default = store
