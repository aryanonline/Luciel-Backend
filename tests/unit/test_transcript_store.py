"""LocalTranscriptStore semantics + the archive key convention (Commitment #38)."""

from __future__ import annotations

import uuid

import pytest
from app.lifecycle.transcript_store import (
    LocalTranscriptStore,
    transcript_key_for,
    transcript_prefix_for,
)

pytestmark = [pytest.mark.asyncio]

ADMIN = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
OTHER = uuid.UUID("00000000-0000-0000-0000-0000000000bb")
IID = uuid.uuid4()


@pytest.fixture(autouse=True)
def _fresh_store():
    LocalTranscriptStore.reset()
    yield
    LocalTranscriptStore.reset()


def test_key_convention_nests_under_the_tenant_prefix():
    """The cascade deletes by tenant prefix, so every key MUST live under it."""
    sid = uuid.uuid4()
    key = transcript_key_for(ADMIN, IID, sid)
    assert key.startswith(transcript_prefix_for(ADMIN))
    assert key.endswith(f"{sid}.jsonl")


async def test_put_get_delete_roundtrip():
    store = LocalTranscriptStore()
    key = transcript_key_for(ADMIN, IID, uuid.uuid4())
    await store.put(key=key, data=b"line-1\n")
    assert await store.get(key=key) == b"line-1\n"
    await store.delete(key=key)
    assert await store.get(key=key) is None
    # Idempotent delete: an absent key is a no-op, not an error.
    await store.delete(key=key)


async def test_put_is_overwrite_idempotent():
    """The archive sweep is at-least-once; a re-put of the same session key must
    replace, never duplicate."""
    store = LocalTranscriptStore()
    key = transcript_key_for(ADMIN, IID, uuid.uuid4())
    await store.put(key=key, data=b"v1\n")
    await store.put(key=key, data=b"v2\n")
    assert await store.get(key=key) == b"v2\n"
    assert len(LocalTranscriptStore.keys()) == 1


async def test_delete_prefix_removes_only_the_tenants_objects():
    """Tenant-prefix deletion (the cascade's handle) must never cross tenants."""
    store = LocalTranscriptStore()
    mine = [transcript_key_for(ADMIN, IID, uuid.uuid4()) for _ in range(3)]
    theirs = transcript_key_for(OTHER, IID, uuid.uuid4())
    for k in mine:
        await store.put(key=k, data=b"x")
    await store.put(key=theirs, data=b"y")

    deleted = await store.delete_prefix(prefix=transcript_prefix_for(ADMIN))

    assert deleted == 3
    assert LocalTranscriptStore.keys() == [theirs]
