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
