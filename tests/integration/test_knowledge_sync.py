"""Live-sync connector mechanics (Architecture §3.2.3, Arc 11b).

Uses a deterministic in-memory SyncProvider to exercise the full incremental
machinery without external APIs: initial sync ingests; changed-revision items are
re-ingested (chunks replaced); items removed at source are removed from the KB
(so Luciel stops answering from deleted content); revocation flags paused_reconnect_needed.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.knowledge import service, sync
from app.knowledge.sync import RemoteItem, RemoteSnapshot
from app.models.instance import Account, AccountState, Instance, LucielState

pytestmark = [pytest.mark.asyncio]


class FakeDriveProvider:
    origin = "google_drive"

    def __init__(self, items: list[RemoteItem]) -> None:
        self._items = items

    def set_items(self, items: list[RemoteItem]) -> None:
        self._items = items

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        return RemoteSnapshot(items=list(self._items))


async def _seed(admin_id: uuid.UUID) -> uuid.UUID:
    async with tenant_session(admin_id) as s:
        s.add(
            Account(
                id=admin_id,
                admin_id=admin_id,
                user_id=admin_id,
                email=f"{admin_id}@x.com",
                email_lower=f"{admin_id}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        iid = uuid.uuid4()
        s.add(
            Instance(
                id=iid,
                admin_id=admin_id,
                name="L",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
        return iid


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_initial_change_and_delete_propagation():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    conn = uuid.uuid4()
    provider = FakeDriveProvider(
        [
            RemoteItem(
                external_id="doc1", name="Playbook A", text="alpha pricing 899", revision="r1"
            ),
            RemoteItem(
                external_id="doc2", name="Playbook B", text="beta hours weekdays", revision="r1"
            ),
        ]
    )

    # Initial sync ingests both.
    async with tenant_session(admin) as s:
        res = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=conn,
            origin="google_drive",
            provider=provider,
        )
        assert set(res.added) == {"doc1", "doc2"}
    async with tenant_session(admin) as s:
        sources = await service.list_sources(s, admin)
        assert len(sources) == 2
        assert all(x.sync_status == "synced" and x.last_synced_at is not None for x in sources)

    # Change doc1 (new revision) and remove doc2 at source.
    provider.set_items(
        [
            RemoteItem(
                external_id="doc1",
                name="Playbook A",
                text="alpha pricing now 999 updated",
                revision="r2",
            ),
        ]
    )
    async with tenant_session(admin) as s:
        res = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=conn,
            origin="google_drive",
            provider=provider,
        )
        assert res.updated == ["doc1"]
        assert res.removed == ["doc2"]

    # doc2 gone from retrieval; doc1 now reflects updated text.
    async with tenant_session(admin) as s:
        active = await service.list_sources(s, admin)
        ext_ids = {x.external_id for x in active}
        assert ext_ids == {"doc1"}
        results = await service.retrieve(
            s, admin_id=admin, instance_id=iid, query="alpha pricing 999 updated", k=3
        )
        assert results and "999" in results[0][0].text


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_revocation_flags_paused_reconnect_needed_not_silent_stale():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    conn = uuid.uuid4()
    provider = FakeDriveProvider(
        [
            RemoteItem(external_id="d1", name="Doc", text="content here", revision="r1"),
        ]
    )
    async with tenant_session(admin) as s:
        await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=conn,
            origin="google_drive",
            provider=provider,
        )
    async with tenant_session(admin) as s:
        n = await sync.pause_sync_on_revocation(s, admin_id=admin, connection_id=conn)
        assert n == 1
    async with tenant_session(admin) as s:
        sources = await service.list_sources(s, admin)
        # §3.2.3 canonical vocabulary (renamed from needs_reconnect, Axis4-F02).
        assert sources[0].sync_status == "paused_reconnect_needed"
