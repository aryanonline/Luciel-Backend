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
    imports this module cleanly. Bound whenever the CDK injected a real bucket
    (``LUCIEL_EXPORT_BUCKET``), which is every deployed environment — see
    :func:`get_export_store` for why this seam does NOT follow the integration-mode
    gate the sender seams use (round 7 WP-5, item 3).

NEVER hardcode a secret here: the S3 backend takes only a bucket name + region
(config, not credential material) — actual AWS credentials are resolved by the
boto3 default credential chain (IAM role in ECS), never a literal in this file.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

#: The CDK-injected bucket variable whose PRESENCE binds the real store (the settings
#: value ``export_bucket`` always carries a name, so it cannot say "configured").
EXPORT_BUCKET_ENV = "LUCIEL_EXPORT_BUCKET"


def export_prefix_for(admin_id: uuid.UUID) -> str:
    """The tenant's bundle prefix — the hard-delete cascade's one-sweep deletion handle
    (round 7 WP-5, item 4), mirroring ``transcript_prefix_for``."""
    return f"vm-export-bundles/{admin_id}/"


def export_key_for(admin_id: uuid.UUID, export_id: uuid.UUID) -> str:
    """Build the per-admin S3 key (§5.10: "a per-admin prefix").

    Mirrors the ``secret_ref_for`` convention in ``app/connections/secrets.py``
    so the tenant-scoping shape is consistent across every AWS-backed seam."""
    return f"{export_prefix_for(admin_id)}{export_id}.zip"


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

    async def exists(self, *, key: str) -> bool:
        """Whether ``key`` is still in the store. ``False`` ONLY when the store
        answered that it is absent; a store that could not answer raises.

        The download door asks before it presigns (round 7 WP-5, item 8): a presign
        is computed locally and "succeeds" for an object the hard-delete cascade
        swept, so a still-valid 7-day link redirected the owner to the bucket's raw
        error page instead of the honest "this link is no longer valid"."""
        ...

    async def put_and_presign(self, *, key: str, data: bytes, ttl_days: int) -> str:
        """Legacy one-shot: upload then presign for ``ttl_days`` (see class docstring
        for why the export flow no longer uses it)."""
        ...

    async def delete_prefix(self, *, prefix: str) -> int:
        """Delete every bundle under ``prefix``; return how many keys were removed.

        The hard-delete cascade's handle (round 7 WP-5, item 4): a bundle is every
        table the tenant owns in one object, so it cannot outlive the rows."""
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

    async def exists(self, *, key: str) -> bool:
        with self._lock:
            return key in type(self)._store

    async def put_and_presign(self, *, key: str, data: bytes, ttl_days: int) -> str:
        await self.put(key=key, data=data)
        return await self.presign(key=key, ttl_seconds=ttl_days * 24 * 60 * 60)

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
    module and runs the LocalExportStore tests, and the client is built on first use
    so merely BINDING this store needs no credentials. Selected by
    :func:`get_export_store` whenever a real bucket was injected; never the default."""

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

    async def exists(self, *, key: str) -> bool:
        """HeadObject. Absent is S3's own no-such-object answer (404 / NotFound /
        NoSuchKey — the task role holds ListBucket, so a missing key is a 404 and not
        a 403); anything else is the store failing to answer and is raised, never read
        as "gone"."""
        from app.lifecycle.transcript_store import (  # noqa: PLC0415 — one S3 dialect
            _is_absent as s3_says_absent,
        )

        try:
            self._get_client().head_object(  # type: ignore[attr-defined]
                Bucket=self._bucket, Key=key
            )
        except Exception as exc:
            if s3_says_absent(exc):
                return False
            raise
        return True

    async def put_and_presign(self, *, key: str, data: bytes, ttl_days: int) -> str:
        await self.put(key=key, data=data)
        return await self.presign(key=key, ttl_seconds=ttl_days * 24 * 60 * 60)

    async def delete_prefix(self, *, prefix: str) -> int:
        """Destroy every object under ``prefix``, every version and delete marker
        included (the same enumeration ``S3TranscriptStore`` uses): on a versioned
        bucket a plain delete only writes a marker and the full tenant dump persists
        as a noncurrent version. Returns the number of distinct KEYS removed."""
        client = self._get_client()
        keys_removed: set[str] = set()
        kwargs: dict[str, object] = {"Bucket": self._bucket, "Prefix": prefix}
        while True:
            page = client.list_object_versions(**kwargs)  # type: ignore[attr-defined]
            doomed = [
                {"Key": item["Key"], "VersionId": item["VersionId"]}
                for section in ("Versions", "DeleteMarkers")
                for item in page.get(section, [])
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


_default: ExportStore | None = None


def get_export_store() -> ExportStore:
    """Return the bound ExportStore: the real bucket when the INFRASTRUCTURE gave us
    one, the local fake when it did not (mirrors ``transcript_store.get_transcript_store``).

    Deliberately NOT keyed on ``integration_mode`` (round 7 WP-5, item 3). That flag
    means "may this process talk to third parties that cost money and reach real
    people"; S3 is our own storage and exists in every deployed environment. Keyed on
    the mode, every deployment running ``mock`` — dev always, production up to the
    owner-gated live cutover — bound the in-process fake: the export "uploaded" into
    one task's memory, the 7-day link the owner was emailed resolved to nothing from
    any other task (or after the next deploy), and the closure's export offer was a
    promise the platform could not keep. Every sibling object-store seam (transcripts,
    knowledge originals, voice audio) already keys on its CDK-injected bucket variable.

    The discriminator is the environment variable rather than the settings value,
    because the setting carries a placeholder default: a bucket NAME is always present,
    so it cannot distinguish "configured" from "defaulted". Local runs and CI set no
    such variable and keep the fake, so the suite stays hermetic. A LIVE process with
    no bucket is a misconfiguration ``integration_status.degraded_bindings`` names as
    ``export_store:unbound`` rather than something this function papers over.
    """
    global _default
    if _default is None:
        import os  # noqa: PLC0415 — deployment shape, read at bind time

        from app.config import get_settings  # noqa: PLC0415 — avoid import cycle

        settings = get_settings()
        if os.environ.get(EXPORT_BUCKET_ENV):
            _default = S3ExportStore(bucket=settings.export_bucket, region_name=settings.aws_region)
        else:
            _default = LocalExportStore()
    return _default


def set_export_store(store: ExportStore | None) -> None:
    """Override the store (tests inject a fresh fake; None resets to default)."""
    global _default
    _default = store
