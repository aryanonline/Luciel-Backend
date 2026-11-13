"""One reconcile per connection: the advisory lock, against real Postgres (round 7).

The lock is what says a sync job is alive. ``pg_locks`` shows a two-key advisory lock as
unsigned ``oid`` columns holding the bits of the signed int4 keys, so a sloppy conversion
would read every negative key as "no job": a live job as interrupted, and a second
reconcile admitted beside it, which duplicates sources.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.knowledge import sync
from app.knowledge.sync import RemoteItem, RemoteSnapshot, SyncAlreadyRunning
from sqlalchemy import text


def _uuid_with_prefix(prefix: str) -> uuid.UUID:
    return uuid.UUID(prefix + uuid.uuid4().hex[8:])


@pytest.mark.parametrize(
    ("prefix", "signed_k2"),
    [
        pytest.param("00000000", -(2**31), id="lowest-negative"),
        pytest.param("7fffffff", -1, id="highest-negative"),
        pytest.param("80000000", 0, id="zero"),
        pytest.param("ffffffff", 2**31 - 1, id="highest-positive"),
    ],
)
async def test_a_held_lock_reads_back_as_its_signed_key(prefix: str, signed_k2: int) -> None:
    connection_id = _uuid_with_prefix(prefix)
    assert sync.sync_lock_k2(connection_id) == signed_k2
    admin = uuid.uuid4()

    async with tenant_session(admin) as holder:
        assert await sync.claim_sync_lock(holder, connection_id)
        async with tenant_session(admin) as reader:
            assert signed_k2 in await sync.live_sync_lock_keys(reader)

    async with tenant_session(admin) as reader:
        assert signed_k2 not in await sync.live_sync_lock_keys(reader)


async def test_another_namespaces_lock_on_the_same_key_is_not_a_job() -> None:
    connection_id = uuid.uuid4()
    admin = uuid.uuid4()
    other_k1 = sync.SYNC_LOCK_K1 + 1 if sync.SYNC_LOCK_K1 < 2**31 - 1 else sync.SYNC_LOCK_K1 - 1

    async with tenant_session(admin) as holder:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(:k1, :k2)"),
            {"k1": other_k1, "k2": sync.sync_lock_k2(connection_id)},
        )
        async with tenant_session(admin) as reader:
            assert sync.sync_lock_k2(connection_id) not in await sync.live_sync_lock_keys(reader)


async def test_the_namespace_is_its_own() -> None:
    """A collision with the sweeps', the claims' or the exports' first key would read their
    locks as sync jobs."""
    import zlib

    from app.jobs import sweeps
    from app.lifecycle import export_bundle

    kinds = [v for k, v in vars(sweeps).items() if k.isupper() and isinstance(v, str)]
    assert kinds
    kinds += ["dest_claim", export_bundle._EXPORT_LOCK_KIND]
    assert sync.SYNC_LOCK_K1 not in {zlib.crc32(kind.encode()) - 2**31 for kind in kinds}


async def test_a_second_transaction_is_refused_and_the_first_can_retake_it() -> None:
    connection_id = uuid.uuid4()
    admin = uuid.uuid4()

    async with tenant_session(admin) as first:
        assert await sync.claim_sync_lock(first, connection_id)
        assert await sync.claim_sync_lock(first, connection_id)  # reentrant
        async with tenant_session(admin) as second:
            assert not await sync.claim_sync_lock(second, connection_id)

    async with tenant_session(admin) as later:
        assert await sync.claim_sync_lock(later, connection_id)  # released with the commit


async def test_a_rolled_back_savepoint_keeps_a_lock_taken_before_it() -> None:
    connection_id = uuid.uuid4()
    admin = uuid.uuid4()

    async with tenant_session(admin) as holder:
        assert await sync.claim_sync_lock(holder, connection_id)
        with pytest.raises(RuntimeError):
            async with holder.begin_nested():
                assert await sync.claim_sync_lock(holder, connection_id)
                raise RuntimeError("the sync failed")
        async with tenant_session(admin) as other:
            assert not await sync.claim_sync_lock(other, connection_id)


async def test_sync_connection_refuses_as_its_first_statement() -> None:
    class _NeverAsked:
        origin = "website_crawl"
        calls = 0

        async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
            type(self).calls += 1
            return RemoteSnapshot(
                items=[RemoteItem(external_id="x", name="x", text="x", revision="r")]
            )

    connection_id = uuid.uuid4()
    admin = uuid.uuid4()

    async with tenant_session(admin) as holder:
        assert await sync.claim_sync_lock(holder, connection_id)
        async with tenant_session(admin) as other:
            with pytest.raises(SyncAlreadyRunning):
                await sync.sync_connection(
                    other,
                    admin_id=admin,
                    instance_id=uuid.uuid4(),
                    connection_id=connection_id,
                    origin="website_crawl",
                    provider=_NeverAsked(),
                )
    assert _NeverAsked.calls == 0
