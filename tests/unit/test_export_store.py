"""LocalExportStore behavior (mirrors LocalSecretStore tests) — Architecture
§3.6.6 / §5.10. Deterministic, in-process, NO network call.
"""

from __future__ import annotations

import uuid

from app.lifecycle.export_store import LocalExportStore, export_key_for


def test_export_key_is_per_admin_prefixed() -> None:
    admin_id = uuid.uuid4()
    export_id = uuid.uuid4()
    key = export_key_for(admin_id, export_id)
    assert key == f"vm-export-bundles/{admin_id}/{export_id}.zip"


def test_local_export_store_roundtrip_and_ttl() -> None:
    LocalExportStore.reset()
    store = LocalExportStore()

    async def run() -> str:
        return await store.put_and_presign(key="k1", data=b"zip-bytes", ttl_days=7)

    import asyncio

    url = asyncio.run(run())
    # Shaped like a presigned link: carries the key and an expiry marker.
    assert "k1" in url
    assert "expires=" in url
    # The bytes are retrievable via the test hook (proves the bundle wasn't discarded).
    assert LocalExportStore.get_bytes("k1") == b"zip-bytes"


def test_local_export_store_reset_clears_state() -> None:
    LocalExportStore.reset()
    store = LocalExportStore()

    import asyncio

    asyncio.run(store.put_and_presign(key="k2", data=b"x", ttl_days=7))
    assert LocalExportStore.get_bytes("k2") == b"x"
    LocalExportStore.reset()
    assert LocalExportStore.get_bytes("k2") is None


# --- delete_prefix (round 7 WP-5, item 4) ------------------------------------------ #
def test_local_delete_prefix_removes_only_the_tenants_bundles() -> None:
    """Tenant-prefix deletion (the cascade's handle) must never cross tenants."""
    from app.lifecycle.export_store import export_prefix_for

    LocalExportStore.reset()
    store = LocalExportStore()
    admin, other = uuid.uuid4(), uuid.uuid4()
    mine = [export_key_for(admin, uuid.uuid4()) for _ in range(2)]
    theirs = export_key_for(other, uuid.uuid4())

    import asyncio

    async def run() -> int:
        for k in mine:
            await store.put(key=k, data=b"zip")
        await store.put(key=theirs, data=b"zip")
        return await store.delete_prefix(prefix=export_prefix_for(admin))

    assert asyncio.run(run()) == 2
    assert LocalExportStore.keys() == [theirs]
    assert export_key_for(admin, uuid.uuid4()).startswith(export_prefix_for(admin))


class _RecordingS3Client:
    """Stub boto3 client modeling a VERSIONED bucket's list/delete surface (mirrors
    the transcript-store stub). Seeded with (key, version_id, is_delete_marker)."""

    def __init__(self, versions: list[tuple[str, str, bool]]) -> None:
        self._versions = list(versions)
        self.deleted: list[dict[str, str]] = []

    def list_object_versions(self, **kwargs):
        prefix = kwargs.get("Prefix", "")
        matching = [v for v in self._versions if v[0].startswith(prefix)]
        return {
            "Versions": [{"Key": k, "VersionId": v} for k, v, marker in matching if not marker],
            "DeleteMarkers": [{"Key": k, "VersionId": v} for k, v, marker in matching if marker],
            "IsTruncated": False,
        }

    def delete_objects(self, *, Bucket, Delete):  # noqa: N803 — boto3's casing
        self.deleted.extend(Delete["Objects"])


def test_s3_delete_prefix_destroys_every_version_under_the_tenant_prefix() -> None:
    """A versioned bucket keeps a noncurrent copy of the full tenant dump behind a
    plain delete; the sweep enumerates versions AND markers, tenant prefix only."""
    from app.lifecycle.export_store import S3ExportStore, export_prefix_for

    admin, other = uuid.uuid4(), uuid.uuid4()
    k1, k2 = export_key_for(admin, uuid.uuid4()), export_key_for(admin, uuid.uuid4())
    theirs = export_key_for(other, uuid.uuid4())
    client = _RecordingS3Client(
        [(k1, "v1", False), (k1, "v0", True), (k2, "v1", False), (theirs, "v1", False)]
    )
    store = S3ExportStore(bucket="b", region_name="ca-central-1")
    store._client = client

    import asyncio

    assert asyncio.run(store.delete_prefix(prefix=export_prefix_for(admin))) == 2
    assert {d["Key"] for d in client.deleted} == {k1, k2}
    assert len(client.deleted) == 3, "both versions of k1 plus its marker"


# --- binding (round 7 WP-5, item 3) ------------------------------------------------ #
def _rebind(monkeypatch, *, bucket: str | None, mode: str):
    """Bind a fresh store under a given deployment shape (mirrors test_transcript_store)."""
    from app.config import get_settings
    from app.lifecycle import export_store as mod

    monkeypatch.setenv("LUCIEL_INTEGRATION_MODE", mode)
    if bucket is None:
        monkeypatch.delenv("LUCIEL_EXPORT_BUCKET", raising=False)
    else:
        monkeypatch.setenv("LUCIEL_EXPORT_BUCKET", bucket)
    get_settings.cache_clear()  # type: ignore[attr-defined]
    mod.set_export_store(None)
    try:
        return mod.get_export_store()
    finally:
        mod.set_export_store(None)
        get_settings.cache_clear()  # type: ignore[attr-defined]


def test_a_deployed_environment_binds_the_real_bucket_even_in_mock_mode(monkeypatch) -> None:
    """The store used to bind on ``integration_mode == "live"``. Dev is always ``mock``
    and production is ``mock`` until the owner-gated live cutover, so in BOTH the export
    "uploaded" into one ECS task's memory and the 7-day link the owner was emailed
    resolved to nothing from any other task. Every sibling object-store seam keys on
    the CDK-injected bucket variable; now this one does too."""
    store = _rebind(monkeypatch, bucket="luciel-dev-export-bundles-000", mode="mock")

    assert type(store).__name__ == "S3ExportStore"
    assert store._client is None, "binding must not build a boto3 client (CI has no AWS)"


def test_a_local_run_keeps_the_in_process_fake(monkeypatch) -> None:
    """No injected bucket means no AWS: the suite and docker-compose stay hermetic."""
    store = _rebind(monkeypatch, bucket=None, mode="mock")

    assert type(store).__name__ == "LocalExportStore"


def test_live_mode_without_a_bucket_does_not_guess_a_bucket_name(monkeypatch) -> None:
    """The settings default ``vm-export-bundles`` is a placeholder, not a deployment fact:
    the old binding pointed live at that literal name. Now the fake is bound and the
    misconfiguration is named by ``integration_status`` (export_store:unbound)."""
    from tests.unit.live_settings import LIVE_PLATFORM_SECRET

    monkeypatch.setenv("LUCIEL_PLATFORM_SECRET", LIVE_PLATFORM_SECRET)
    store = _rebind(monkeypatch, bucket=None, mode="live")

    assert type(store).__name__ == "LocalExportStore"
