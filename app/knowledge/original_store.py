"""OriginalStore — the customer's uploaded files, kept as they were uploaded.

§5.10 / §3.6.6 promise the account export contains "your original uploaded files",
and the bundle's own README said so, but nothing ever stored them (2026-09-05
audit F016): the upload route parsed the bytes into chunks and dropped them. This
seam keeps the bytes, under a per-tenant prefix, so the export can hand them back
and the deletion paths (source purge, tenant cascade) can destroy them.

Mirrors ``lifecycle/transcript_store.py`` exactly — the same mock/live gate
(bound to S3 when the CDK injected a bucket, the in-process fake otherwise), the
same ``vm/{admin_id}/`` tenant prefix discipline (one prefix sweep at the cascade).

Key convention: ``vm/{admin_id}/knowledge/originals/{source_id}/{filename}``.
"""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")


def original_prefix_for(admin_id: uuid.UUID) -> str:
    """The tenant's originals prefix — the cascade's one-sweep deletion handle."""
    return f"vm/{admin_id}/knowledge/originals/"


def safe_filename(name: str) -> str:
    """A filename that cannot escape its key prefix and stays readable in a ZIP."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    base = _SAFE_NAME.sub("_", base).strip("._ ") or "file"
    return base[:200]


def original_key_for(admin_id: uuid.UUID, source_id: uuid.UUID, filename: str) -> str:
    return f"{original_prefix_for(admin_id)}{source_id}/{safe_filename(filename)}"


@dataclass(frozen=True)
class StoredOriginal:
    data: bytes
    content_type: str
    # When the bytes were written (S3: the object's LastModified). The retention
    # sweep's orphan reaper reads it to leave an in-flight upload alone.
    stored_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class ListedOriginal:
    """One object under a prefix, as ``list_prefix`` reports it: the key and when it
    was last written. The store reports facts; which of them are orphans is the
    retention sweep's policy (app/lifecycle/retention.py)."""

    key: str
    last_modified: datetime


@runtime_checkable
class OriginalStore(Protocol):
    """Persist / read / delete the customer's original uploaded files."""

    async def put(self, *, key: str, data: bytes, content_type: str) -> None: ...

    async def get(self, *, key: str) -> StoredOriginal | None: ...

    async def delete(self, *, key: str) -> None: ...

    async def delete_prefix(self, *, prefix: str) -> int: ...

    async def list_prefix(self, *, prefix: str) -> list[ListedOriginal]:
        """Every object under ``prefix`` with its last-written time (tz-aware) — the
        retention sweep's orphan reconciliation reads the tenant's prefix and compares
        it against the knowledge_sources rows (round 7 WP-8)."""
        ...


class LocalOriginalStore:
    """In-process, deterministic OriginalStore for dev/test (mirrors LocalTranscriptStore)."""

    _store: dict[str, StoredOriginal] = {}
    _lock = threading.Lock()

    async def put(self, *, key: str, data: bytes, content_type: str) -> None:
        with self._lock:
            type(self)._store[key] = StoredOriginal(bytes(data), content_type)

    async def get(self, *, key: str) -> StoredOriginal | None:
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

    async def list_prefix(self, *, prefix: str) -> list[ListedOriginal]:
        with self._lock:
            return [
                ListedOriginal(k, v.stored_at)
                for k, v in sorted(type(self)._store.items())
                if k.startswith(prefix)
            ]

    @classmethod
    def keys(cls) -> list[str]:
        with cls._lock:
            return sorted(cls._store)

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._store.clear()


class S3OriginalStore:
    """Real originals backend: plain (unversioned) objects, SSE-KMS, no lifecycle expiry —
    an original lives exactly as long as its source. boto3 imported lazily (CI has no
    AWS); the client is built on first use so merely binding needs no credentials."""

    def __init__(self, *, bucket: str, region_name: str) -> None:
        self._bucket = bucket
        self._region_name = region_name
        self._client: object | None = None

    def _get_client(self) -> object:
        if self._client is None:
            import boto3  # noqa: PLC0415 — lazy/guarded import (CI has no AWS)

            self._client = boto3.client("s3", region_name=self._region_name)
        return self._client

    async def put(self, *, key: str, data: bytes, content_type: str) -> None:
        self._get_client().put_object(  # type: ignore[attr-defined]
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type or "application/octet-stream",
            ServerSideEncryption="aws:kms",
        )

    async def get(self, *, key: str) -> StoredOriginal | None:
        client = self._get_client()
        try:
            resp = client.get_object(Bucket=self._bucket, Key=key)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — NoSuchKey and transport errors both read as absent
            return None
        return StoredOriginal(
            bytes(resp["Body"].read()), str(resp.get("ContentType") or "application/octet-stream")
        )

    async def delete(self, *, key: str) -> None:
        self._get_client().delete_object(Bucket=self._bucket, Key=key)  # type: ignore[attr-defined]

    async def delete_prefix(self, *, prefix: str) -> int:
        client = self._get_client()
        removed = 0
        kwargs: dict[str, object] = {"Bucket": self._bucket, "Prefix": prefix}
        while True:
            page = client.list_objects_v2(**kwargs)  # type: ignore[attr-defined]
            doomed = [{"Key": item["Key"]} for item in page.get("Contents", [])]
            if doomed:
                client.delete_objects(  # type: ignore[attr-defined]
                    Bucket=self._bucket, Delete={"Objects": doomed, "Quiet": True}
                )
                removed += len(doomed)
            if not page.get("IsTruncated"):
                return removed
            kwargs["ContinuationToken"] = page.get("NextContinuationToken")

    async def list_prefix(self, *, prefix: str) -> list[ListedOriginal]:
        client = self._get_client()
        listed: list[ListedOriginal] = []
        kwargs: dict[str, object] = {"Bucket": self._bucket, "Prefix": prefix}
        while True:
            page = client.list_objects_v2(**kwargs)  # type: ignore[attr-defined]
            for item in page.get("Contents", []):
                # boto3 hands back a tz-aware LastModified; a naive one is UTC.
                modified: datetime = item["LastModified"]
                if modified.tzinfo is None:
                    modified = modified.replace(tzinfo=UTC)
                listed.append(ListedOriginal(str(item["Key"]), modified))
            if not page.get("IsTruncated"):
                return listed
            kwargs["ContinuationToken"] = page.get("NextContinuationToken")


_default: OriginalStore | None = None


def get_original_store() -> OriginalStore:
    """The real bucket when the INFRASTRUCTURE gave us one, the local fake when it did
    not — keyed on the CDK-injected env var, not ``integration_mode`` (see
    ``transcript_store.get_transcript_store`` for why)."""
    global _default
    if _default is None:
        import os  # noqa: PLC0415 — deployment shape, read at bind time

        from app.config import get_settings  # noqa: PLC0415 — avoid import cycle

        settings = get_settings()
        if os.environ.get("LUCIEL_KNOWLEDGE_ORIGINALS_BUCKET"):
            _default = S3OriginalStore(
                bucket=settings.knowledge_originals_bucket, region_name=settings.aws_region
            )
        else:
            _default = LocalOriginalStore()
    return _default


def set_original_store(store: OriginalStore | None) -> None:
    """Override the store (tests inject a fresh fake; None resets to default)."""
    global _default
    _default = store
