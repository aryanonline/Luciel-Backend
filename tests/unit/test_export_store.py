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
