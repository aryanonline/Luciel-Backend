"""A knowledge sync in flight when the Luciel is paused or deleted (round 7 M10).

Live gap: the knowledge sweep skips a non-serving Luciel when it STARTS, but the
provider fetch can take minutes (a website crawl). An owner who paused or deleted the
Luciel while the fetch was on the wire got the run's writes anyway: new chunks
embedded and stored, changed pages re-ingested, absent pages tombstoned — for a
Luciel that was serving nobody (and, when deleted, into rows the cascade is about to
purge).

Now the serving state is re-read after ``provider.snapshot()`` returns and before
any write. The fake provider below flips the instance state INSIDE its snapshot, in
its own committed transaction — exactly where the live system spends its seconds.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db.session import tenant_session
from app.knowledge import sweep as knowledge_sweep
from app.knowledge import sync
from app.knowledge.sync import RemoteItem, RemoteSnapshot
from app.lifecycle import state_machine
from app.models.audit import AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.knowledge import KnowledgeChunk, KnowledgeSource
from sqlalchemy import func, select

pytestmark = [pytest.mark.asyncio]

FIRST = [
    RemoteItem(external_id="doc1", name="Hours", text="open weekdays nine to five", revision="r1"),
    RemoteItem(external_id="doc2", name="Prices", text="consultation costs 120", revision="r1"),
]
# What the source says the second time: doc1 changed, doc2 removed, doc3 added. Every
# kind of write the reconcile can make would happen if the run were allowed through.
SECOND = [
    RemoteItem(external_id="doc1", name="Hours", text="open weekends too now", revision="r2"),
    RemoteItem(external_id="doc3", name="Parking", text="free parking at the back", revision="r1"),
]


async def _seed() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    admin, iid, cid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin}@x.com",
                email_lower=f"{admin}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="L",
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{uuid.uuid4().hex[:10]}",
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        s.add(
            InstanceConnection(
                connection_id=cid,
                admin_id=admin,
                instance_id=iid,
                connection_type="knowledge_source",
                auth_class="oauth_token",
                provider="google_drive",
                status="connected",
                non_secret_config={},
            )
        )
    return admin, iid, cid


class _Drive:
    origin = "google_drive"

    def __init__(self, items: list[RemoteItem]) -> None:
        self.items = items
        self.calls = 0

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        self.calls += 1
        return RemoteSnapshot(items=list(self.items))


class _FlipsDuringFetch(_Drive):
    """The owner pauses / deletes the Luciel while the fetch is on the wire."""

    def __init__(self, admin: uuid.UUID, flip: str) -> None:
        super().__init__(SECOND)
        self.admin = admin
        self.flip = flip

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        async with tenant_session(self.admin) as other:
            if self.flip == "pause":
                await state_machine.pause(other, self.admin, actor_user_id=self.admin)
            else:
                await state_machine.delete_luciel(other, self.admin, self.admin)
        return await super().snapshot(connection_id)


async def _first_sync(admin: uuid.UUID, iid: uuid.UUID, cid: uuid.UUID) -> None:
    async with tenant_session(admin) as s:
        result = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=cid,
            origin="google_drive",
            provider=_Drive(FIRST),
        )
    assert sorted(result.added) == ["doc1", "doc2"]


async def _state(admin: uuid.UUID) -> dict[str, object]:
    """Everything a reconcile could have written, in one comparable value."""
    async with tenant_session(admin) as s:
        sources = (
            (await s.execute(select(KnowledgeSource).order_by(KnowledgeSource.external_id)))
            .scalars()
            .all()
        )
        chunks = (await s.execute(select(func.count()).select_from(KnowledgeChunk))).scalar_one()
        sync_audits = [
            p
            for (p,) in (
                await s.execute(
                    select(AdminAuditLog.event_payload).where(
                        AdminAuditLog.event_type == "knowledge_source_edited"
                    )
                )
            ).all()
            if p.get("action") in {"sync", "sync_failed", "sync_refused_shrink"}
        ]
    return {
        "sources": [
            (
                src.external_id,
                src.original_object_key,
                src.sync_status,
                src.soft_deleted_at is None,
                src.paused_since,
                src.last_synced_at,
            )
            for src in sources
            if src.deleted_by != "lifecycle"
        ],
        "chunks": chunks,
        "sync_audits": len(sync_audits),
    }


@pytest.mark.parametrize("flip", ["pause", "delete"])
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_sync_whose_luciel_stops_serving_during_the_fetch_writes_nothing(flip):
    admin, iid, cid = await _seed()
    await _first_sync(admin, iid, cid)
    before = await _state(admin)

    provider = _FlipsDuringFetch(admin, flip)
    async with tenant_session(admin) as s:
        result = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=cid,
            origin="google_drive",
            provider=provider,
        )
    assert provider.calls == 1
    assert result.halted == sync.HALTED_NOT_SERVING
    assert (result.added, result.updated, result.removed, result.refused) == ([], [], [], [])

    after = await _state(admin)
    if flip == "pause":
        # Nothing the reconcile would have done happened: doc1 keeps revision r1 and
        # its chunks, doc2 is not tombstoned, doc3 was never ingested, no status flag
        # (in particular NOT "error" — nothing failed), no sync audit row.
        assert after == before
    else:
        # The delete itself tombstoned the sources (deleted_by="lifecycle", filtered
        # out above); the run added no copy beside them and wrote no chunks.
        assert after["sources"] == []
        assert after["chunks"] == before["chunks"]
        assert after["sync_audits"] == before["sync_audits"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_sweep_run_halts_without_marking_the_sources_failed(monkeypatch):
    admin, iid, cid = await _seed()
    await _first_sync(admin, iid, cid)
    before = await _state(admin)

    provider = _FlipsDuringFetch(admin, "pause")
    monkeypatch.setattr(knowledge_sweep, "provider_for_connection", lambda c, store: provider)
    later = datetime.now(UTC) + timedelta(days=1)
    pending: list[knowledge_sweep.PendingStamp] = []
    async with tenant_session(admin) as s:
        await knowledge_sweep.sync_due_connections(
            s, admin_id=admin, now=later, pending_stamps=pending
        )
    await knowledge_sweep.write_pending_stamps(admin, pending)
    assert provider.calls == 1
    assert await _state(admin) == before

    # Resumed: the next pass reconciles everything the halted run left alone.
    async with tenant_session(admin) as s:
        await state_machine.resume(s, admin, actor_user_id=admin)
    resumed = _Drive(SECOND)
    monkeypatch.setattr(knowledge_sweep, "provider_for_connection", lambda c, store: resumed)
    async with tenant_session(admin) as s:
        synced = await knowledge_sweep.sync_due_connections(s, admin_id=admin, now=later)
    assert (synced, resumed.calls) == (1, 1)
    live = {
        src[0]: src[1]
        for src in (await _state(admin))["sources"]
        if src[3]  # not tombstoned
    }
    assert live == {"doc1": "r2", "doc3": "r1"}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_manual_sync_on_an_already_paused_luciel_still_runs(monkeypatch):
    """Nothing changed under a run the owner started on a paused Luciel (preparing
    knowledge before resuming): it reconciles exactly as before."""
    admin, iid, cid = await _seed()
    await _first_sync(admin, iid, cid)
    async with tenant_session(admin) as s:
        await state_machine.pause(s, admin, actor_user_id=admin)

    async with tenant_session(admin) as s:
        result = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=cid,
            origin="google_drive",
            provider=_Drive(SECOND),
        )
    assert result.halted is None
    assert (result.added, result.updated, result.removed) == (["doc3"], ["doc1"], ["doc2"])
