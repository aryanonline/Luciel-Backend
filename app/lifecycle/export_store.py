"""ExportStore — swappable persistence for data-export ZIP bundles (Architecture
§3.6.6 / §5.10).

The bundle is built in-memory by ``export_bundle.build_export_zip`` and then must
be PERSISTED and turned into a time-limited download link (§5.10: "the admin
requests the export, a background job assembles the ZIP and uploads it to
``vm-export-bundles`` S3 with a per-admin prefix, and a time-limited presigned
URL is issued. The download link has a 7-day TTL (consistent with §3.6.6)").

This module mirrors the ``app/connections/secrets.py`` ``get_secret_store`` seam
exactly (same shape, same mock/live gate) so the export path follows the same
AWS-DEFERRED SEAM discipline as the rest of the codebase:

  * :class:`LocalExportStore` — in-process, deterministic fake used in every test
    and in the default ``mock`` integration mode. Holds bytes in a process-global
    dict and mints a fake ``https://export.local/...`` URL — NO network call.
  * :class:`S3ExportStore` — the real ``vm-export-bundles`` S3 backend (boto3
    ``put_object`` + ``generate_presigned_url``), imported LAZILY so CI (no AWS)
    imports this module cleanly. Bound ONLY when ``integration_mode == "live"``
    by :func:`get_export_store`, mirroring ``get_secret_store``.

NEVER hardcode a secret here: the S3 backend takes only a bucket name + region
(config, not credential material) — actual AWS credentials are resolved by the
boto3 default credential chain (IAM role in ECS), never a literal in this file.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


def export_key_for(admin_id: uuid.UUID, export_id: uuid.UUID) -> str:
    """Build the per-admin S3 key prefix (§5.10: "a per-admin prefix").

    Mirrors the ``secret_ref_for`` convention in ``app/connections/secrets.py``
    so the tenant-scoping shape is consistent across every AWS-backed seam."""
    return f"vm-export-bundles/{admin_id}/{export_id}.zip"


@runtime_checkable
class ExportStore(Protocol):
    """Persist an export ZIP and mint short-lived download links (§3.6.6).

    The 7-day promise is carried by the API's signed link token (``/api/v1/exports/
    {token}``), NOT by a 7-day presign: a presigned URL is only valid while the
    credentials that signed it are, and the ECS task role's are rotated within hours
    (2026-09-05 audit F028). Each visit to the token route mints a fresh, minutes-long
    presign instead.
    """

    async def put(self, *, key: str, data: bytes) -> None:
        """Upload ``data`` under ``key``."""
        ...

    async def presign(self, *, key: str, ttl_seconds: int) -> str:
        """A GET URL for ``key`` valid for ``ttl_seconds`` (short: minutes)."""
        ...

    async def put_and_presign(self, *, key: str, data: bytes, ttl_days: int) -> str:
        """Legacy one-shot: upload then presign for ``ttl_days`` (see class docstring
        for why the export flow no longer uses it)."""
        ...


class LocalExportStore:
    """In-process, deterministic ExportStore for dev/test (mirrors LocalSecretStore).

    Holds bundle bytes in a process-global dict so a test can both request the
    export and assert on the bundle contents without any real storage. Never used
    in production — the seam the S3ExportStore replaces 1:1."""

    # Process-global so independent sessions in one test see the same store.
    _store: dict[str, bytes] = {}
    _lock = threading.Lock()

    async def put(self, *, key: str, data: bytes) -> None:
        with self._lock:
            type(self)._store[key] = data

    async def presign(self, *, key: str, ttl_seconds: int) -> str:
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
        # Deterministic fake URL shape: tests assert the host + key + the expiry.
        return f"https://export.local/{key}?expires={int(expires_at.timestamp())}"

    async def put_and_presign(self, *, key: str, data: bytes, ttl_days: int) -> str:
        await self.put(key=key, data=data)
        return await self.presign(key=key, ttl_seconds=ttl_days * 24 * 60 * 60)

    @classmethod
    def get_bytes(cls, key: str) -> bytes | None:
        """Test hook: read back the persisted bundle bytes for content assertions."""
        with cls._lock:
            return cls._store.get(key)

    @classmethod
    def reset(cls) -> None:
        """Test hook: clear the process-global store between tests."""
        with cls._lock:
            cls._store.clear()


class S3ExportStore:
    """Real export-bundle S3 backend (Architecture §3.6.6 / §5.10).

    ``put_object`` uploads the ZIP under the per-admin key; ``generate_presigned_url``
    mints a GET URL valid for ``ttl_days`` (matching ``config.export_link_ttl_days``
    = 7). Server-side encryption + the bucket's own lifecycle expiration rule
    (§4.4 storage-cost bounding) bound the object's life independent of this class —
    a claim that was false until P1-14 provisioned the bucket in CDK: the bucket was
    referenced here and created nowhere, so a ZIP holding a full tenant data dump
    outlived its 7-day link by however long the manual bucket kept it.

    boto3 is imported LAZILY so CI (no AWS, boto3 may be absent) still imports this
    module and runs the LocalExportStore tests. Only selected in ``live`` mode by
    :func:`get_export_store`; never the default."""

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
            # Match on the browser GET). Verified against the live ca-central-1
            # vm-export-bundles bucket.
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

    async def put(self, *, key: str, data: bytes) -> None:
        self._get_client().put_object(  # type: ignore[attr-defined]
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType="application/zip",
            ServerSideEncryption="aws:kms",
        )

    async def presign(self, *, key: str, ttl_seconds: int) -> str:
        url = self._get_client().generate_presigned_url(  # type: ignore[attr-defined]
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=ttl_seconds,
        )
        return str(url)

    async def put_and_presign(self, *, key: str, data: bytes, ttl_days: int) -> str:
        await self.put(key=key, data=data)
        return await self.presign(key=key, ttl_seconds=ttl_days * 24 * 60 * 60)


_default: ExportStore | None = None


def get_export_store() -> ExportStore:
    """Return the bound ExportStore. Defaults to the local backend (mirrors
    ``app.connections.secrets.get_secret_store``).

    In ``live`` integration mode this binds the AWS ``S3ExportStore`` against the
    ``vm-export-bundles`` bucket; in every other mode (the default ``mock``) it
    stays ``LocalExportStore`` so all existing tests are unaffected."""
    global _default
    if _default is None:
        from app.config import get_settings  # noqa: PLC0415 — avoid import cycle

        settings = get_settings()
        if settings.integration_mode == "live":
            _default = S3ExportStore(bucket=settings.export_bucket, region_name=settings.aws_region)
        else:
            _default = LocalExportStore()
    return _default


def set_export_store(store: ExportStore | None) -> None:
    """Override the store (tests inject a fresh fake; None resets to default)."""
    global _default
    _default = store
