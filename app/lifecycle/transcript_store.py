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
    tier through — with SSE-KMS. Bound whenever the CDK injected a real bucket, which
    is every deployed environment; see :func:`get_transcript_store` for why this seam
    does NOT follow the integration-mode gate the sender seams use.

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

    boto3 is imported LAZILY so CI (no AWS) still imports this module, and the client
    is built on first use so merely BINDING this store needs no credentials. Selected
    by :func:`get_transcript_store` whenever a real bucket was injected."""

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
        """Destroy EVERY version of ``key``, not just the current one.

        The bucket is versioned (accident protection for live accounts), so a plain
        ``delete_object`` only writes a delete marker and the bytes persist as a
        noncurrent version for the 30-day lifecycle tail. For the retention sweep and
        the account-closure cascade — the two callers of this method — that tail is a
        broken promise: the product says deleted data is GONE (owner decision
        2026-08-10). Versioning still protects everything these methods were never
        asked to delete.
        """
        self._delete_all_versions(prefix=key, exact_key=key)

    async def delete_prefix(self, *, prefix: str) -> int:
        """Destroy every object under ``prefix`` including all versions and markers."""
        return self._delete_all_versions(prefix=prefix, exact_key=None)

    def _delete_all_versions(self, *, prefix: str, exact_key: str | None) -> int:
        """Enumerate versions+markers under ``prefix`` and delete them by VersionId.

        ``list_object_versions`` (not ``list_objects_v2``): the current-object listing
        cannot see noncurrent versions or existing delete markers, which is exactly
        what the old implementation left behind. Returns the number of distinct KEYS
        removed (not versions), preserving the sweep's acted-count semantics.
        """
        client = self._get_client()
        keys_removed: set[str] = set()
        kwargs: dict[str, object] = {"Bucket": self._bucket, "Prefix": prefix}
        while True:
            page = client.list_object_versions(**kwargs)  # type: ignore[attr-defined]
            doomed = [
                {"Key": item["Key"], "VersionId": item["VersionId"]}
                for section in ("Versions", "DeleteMarkers")
                for item in page.get(section, [])
                # delete(key=...) must not touch sibling keys that share the prefix
                # (`.../abc.jsonl` vs `.../abc.jsonl.bak`); prefix mode takes all.
                if exact_key is None or item["Key"] == exact_key
            ]
            if doomed:
                client.delete_objects(  # type: ignore[attr-defined]
                    Bucket=self._bucket, Delete={"Objects": doomed, "Quiet": True}
                )
                keys_removed.update(str(d["Key"]) for d in doomed)
            if not page.get("IsTruncated"):
                return len(keys_removed)
            kwargs["KeyMarker"] = page.get("NextKeyMarker")
            kwargs["VersionIdMarker"] = page.get("NextVersionIdMarker")


_default: TranscriptStore | None = None


def get_transcript_store() -> TranscriptStore:
    """Return the bound TranscriptStore: the real bucket when the INFRASTRUCTURE gave
    us one, the local fake when it did not.

    Deliberately NOT keyed on ``integration_mode`` the way the sender seams are. That
    flag means "may this process talk to third parties that cost money and reach real
    people" — Twilio, Meta, Anthropic, Stripe. S3 is not a third party; it is our own
    storage, and it exists in every deployed environment. Keying the archive on the
    mode meant a deployment running ``mock`` (which is dev always, and production up
    to the owner-gated live cutover) bound the in-process fake, so the sweep refused
    and Commitment #38 — transcripts move to cold storage at 90 days — silently never
    ran anywhere. Transcripts instead accumulated in hot Postgres forever, which is
    both the expensive outcome and a promise we were not keeping.

    The discriminator is the CDK-injected environment variable rather than the settings
    value, because the setting carries a placeholder default: a bucket NAME is always
    present, so it cannot distinguish "configured" from "defaulted". Local runs and CI
    set no such variable and keep the fake, so the suite stays hermetic.
    """
    global _default
    if _default is None:
        import os  # noqa: PLC0415 — deployment shape, read at bind time

        from app.config import get_settings  # noqa: PLC0415 — avoid import cycle

        settings = get_settings()
        if os.environ.get("LUCIEL_TRANSCRIPT_ARCHIVE_BUCKET"):
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
