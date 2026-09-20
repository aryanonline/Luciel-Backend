"""Orphaned knowledge originals (round 7 WP-8).

The originals bucket has no expiration by design — an original lives exactly as
long as its source — so an object whose delete failed during a source purge would
survive forever. The retention sweep reconciles the tenant's prefix against the
rows. These are the pure halves: which listed objects count as orphans (ownership
AND a minimum age, because an upload keeps its bytes before its row commits), and
that both store implementations list a prefix with last-written times (the S3 one
across pages).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.knowledge.original_store import (
    ListedOriginal,
    LocalOriginalStore,
    OriginalStore,
    S3OriginalStore,
    original_key_for,
    original_prefix_for,
)
from app.lifecycle.retention import ORIGINAL_ORPHAN_MIN_AGE, orphaned_original_keys

pytestmark = [pytest.mark.asyncio]

NOW = datetime(2026, 9, 20, 3, 0, tzinfo=UTC)
LONG_AGO = NOW - timedelta(days=40)


def _aged(key: str) -> ListedOriginal:
    return ListedOriginal(key, LONG_AGO)


def test_orphans_are_keys_whose_source_row_is_gone_and_nothing_else():
    admin = uuid.uuid4()
    prefix = original_prefix_for(admin)
    live, tombstoned, gone = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    other_tenant_key = original_key_for(uuid.uuid4(), gone, "theirs.pdf")
    listed = [
        _aged(original_key_for(admin, live, "live.pdf")),
        _aged(original_key_for(admin, tombstoned, "undo.pdf")),
        _aged(original_key_for(admin, gone, "orphan.pdf")),
        _aged(f"{prefix}not-a-uuid/stray.bin"),
        _aged(other_tenant_key),
    ]
    # A tombstoned source still HAS a row (its original is the undo payload).
    orphans = orphaned_original_keys(
        listed, prefix=prefix, live_source_ids={live, tombstoned}, modified_before=NOW
    )
    assert orphans == [
        original_key_for(admin, gone, "orphan.pdf"),
        f"{prefix}not-a-uuid/stray.bin",
    ]
    # Another tenant's key is never a candidate, even if handed in.
    assert other_tenant_key not in orphans
    # No rows at all: everything under the prefix is an orphan, nothing outside it.
    everything = orphaned_original_keys(
        listed, prefix=prefix, live_source_ids=set(), modified_before=NOW
    )
    assert set(everything) == {entry.key for entry in listed[:4]}


def test_an_object_written_inside_the_minimum_age_is_kept_whatever_the_rows_say():
    # keep_original runs INSIDE the ingest transaction, so for a moment the bytes
    # exist and the row does not. The sweep never treats that moment as an orphan.
    admin = uuid.uuid4()
    prefix = original_prefix_for(admin)
    in_flight = original_key_for(admin, uuid.uuid4(), "uploading.pdf")
    stale = original_key_for(admin, uuid.uuid4(), "stale.pdf")
    cutoff = NOW - ORIGINAL_ORPHAN_MIN_AGE
    listed = [
        ListedOriginal(in_flight, NOW - timedelta(seconds=5)),
        ListedOriginal(stale, cutoff - timedelta(seconds=1)),
        # Exactly at the cutoff counts as young (>=): the boundary errs on keeping.
        ListedOriginal(original_key_for(admin, uuid.uuid4(), "edge.pdf"), cutoff),
    ]
    assert orphaned_original_keys(
        listed, prefix=prefix, live_source_ids=set(), modified_before=cutoff
    ) == [stale]
    assert ORIGINAL_ORPHAN_MIN_AGE >= timedelta(minutes=10)  # far past any ingest


async def test_local_store_lists_exactly_the_prefix_with_written_times():
    LocalOriginalStore.reset()
    store = LocalOriginalStore()
    assert isinstance(store, OriginalStore)
    a, b = uuid.uuid4(), uuid.uuid4()
    ka = original_key_for(a, uuid.uuid4(), "a.pdf")
    kb = original_key_for(b, uuid.uuid4(), "b.pdf")
    before = datetime.now(UTC)
    await store.put(key=ka, data=b"a", content_type="application/pdf")
    await store.put(key=kb, data=b"b", content_type="application/pdf")
    after = datetime.now(UTC)
    try:
        (entry_a,) = await store.list_prefix(prefix=original_prefix_for(a))
        assert entry_a.key == ka
        assert entry_a.last_modified.tzinfo is not None  # comparable with the sweep's clock
        assert before <= entry_a.last_modified <= after
        assert [e.key for e in await store.list_prefix(prefix=original_prefix_for(b))] == [kb]
        assert await store.list_prefix(prefix=original_prefix_for(uuid.uuid4())) == []
    finally:
        LocalOriginalStore.reset()


class _PagedS3:
    """list_objects_v2 that pages, so the real store's loop is exercised."""

    def __init__(self, objects: dict[str, datetime]) -> None:
        self.objects = objects
        self.calls: list[dict[str, object]] = []

    def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        assert kwargs["Bucket"] == "originals"
        prefix = str(kwargs["Prefix"])
        matching = [(k, t) for k, t in self.objects.items() if k.startswith(prefix)]
        start = int(str(kwargs.get("ContinuationToken", "0")))
        page = matching[start : start + 2]
        truncated = start + 2 < len(matching)
        out: dict[str, object] = {
            "Contents": [{"Key": k, "LastModified": t} for k, t in page],
            "IsTruncated": truncated,
        }
        if truncated:
            out["NextContinuationToken"] = str(start + 2)
        return out


async def test_s3_store_lists_a_prefix_across_pages_with_aware_times():
    admin = uuid.uuid4()
    mine = {
        original_key_for(admin, uuid.uuid4(), f"{i}.pdf"): LONG_AGO + timedelta(minutes=i)
        for i in range(4)
    }
    # boto3 returns tz-aware LastModified; a naive value (some stubs) reads as UTC.
    naive_key = original_key_for(admin, uuid.uuid4(), "naive.pdf")
    mine[naive_key] = LONG_AGO.replace(tzinfo=None)
    theirs = original_key_for(uuid.uuid4(), uuid.uuid4(), "x.pdf")
    store = S3OriginalStore(bucket="originals", region_name="ca-central-1")
    fake = _PagedS3({**mine, theirs: LONG_AGO})
    store._client = fake  # boto3 is never imported: the client is injected
    assert isinstance(store, OriginalStore)
    listed = await store.list_prefix(prefix=original_prefix_for(admin))
    assert sorted(e.key for e in listed) == sorted(mine)
    assert all(e.last_modified.tzinfo is not None for e in listed)
    assert next(e for e in listed if e.key == naive_key).last_modified == LONG_AGO
    assert len(fake.calls) == 3  # 5 keys, 2 per page
