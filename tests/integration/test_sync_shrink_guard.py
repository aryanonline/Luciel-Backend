"""A sync never mass-deletes on a broken read (2026-09-05 audit F107/F117/F108).

An empty listing from a revoked scope, a provider outage answering 200 with
nothing, or an owner who moved a folder used to read as "everything was deleted at
the source" — and the sync retired every source Luciel had learned. Now a snapshot
that would retire more than half of what is synced is refused unless the owner
confirmed it, items the provider could not read this pass are kept and flagged,
and a source that syncs cleanly again drops its old error flag.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.knowledge import sync
from app.knowledge.sync import RemoteItem, RemoteSnapshot, SnapshotShrunk
from app.models.audit import AdminAuditLog
from app.models.instance import Instance, LucielState
from app.models.knowledge import KnowledgeSource
from sqlalchemy import select


class Provider:
    origin = "google_drive"

    def __init__(self, items: list[RemoteItem], failed: list[str] | None = None) -> None:
        self._items = items
        self._failed = failed or []

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        return RemoteSnapshot(items=list(self._items), failed_ids=list(self._failed))


def _items(n: int) -> list[RemoteItem]:
    return [RemoteItem(f"f{i}", f"Doc {i}", f"Document {i} says hello", "r1") for i in range(n)]


async def _seed(admin: uuid.UUID) -> uuid.UUID:
    iid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(Instance(id=iid, admin_id=admin, name="L", state=LucielState.active))
        await s.flush()
    return iid


async def _sync(admin, iid, cid, provider, **kw):
    async with tenant_session(admin) as s:
        return await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=cid,
            origin="google_drive",
            provider=provider,
            **kw,
        )


async def _live(admin: uuid.UUID) -> dict[str, KnowledgeSource]:
    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(KnowledgeSource).where(
                        KnowledgeSource.admin_id == admin,
                        KnowledgeSource.soft_deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        return {r.external_id or "": r for r in rows}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_snapshot_that_would_retire_more_than_half_is_refused():
    admin, cid = uuid.uuid4(), uuid.uuid4()
    iid = await _seed(admin)
    await _sync(admin, iid, cid, Provider(_items(4)))

    with pytest.raises(SnapshotShrunk) as exc:
        await _sync(admin, iid, cid, Provider(_items(1)))
    assert (exc.value.existing, exc.value.remote) == (4, 1)

    live = await _live(admin)
    assert sorted(live) == ["f0", "f1", "f2", "f3"]  # nothing deleted
    assert {src.sync_status for src in live.values()} == {"error"}
    async with tenant_session(admin) as s:
        refusals = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "knowledge_source_edited",
                        AdminAuditLog.event_payload["action"].astext == "sync_refused_shrink",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(refusals) == 1
    assert refusals[0].event_payload["synced"] == 4
    assert refusals[0].event_payload["reported"] == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_empty_snapshot_over_synced_sources_is_refused_too():
    admin, cid = uuid.uuid4(), uuid.uuid4()
    iid = await _seed(admin)
    await _sync(admin, iid, cid, Provider(_items(2)))
    with pytest.raises(SnapshotShrunk):
        await _sync(admin, iid, cid, Provider([]))
    assert sorted(await _live(admin)) == ["f0", "f1"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_owner_can_confirm_an_intentional_shrink():
    admin, cid = uuid.uuid4(), uuid.uuid4()
    iid = await _seed(admin)
    await _sync(admin, iid, cid, Provider(_items(4)))
    res = await _sync(admin, iid, cid, Provider(_items(1)), allow_shrink=True)
    assert sorted(res.removed) == ["f1", "f2", "f3"]
    assert sorted(await _live(admin)) == ["f0"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_modest_diff_still_retires_what_is_gone():
    admin, cid = uuid.uuid4(), uuid.uuid4()
    iid = await _seed(admin)
    await _sync(admin, iid, cid, Provider(_items(4)))
    res = await _sync(admin, iid, cid, Provider(_items(3)))  # one file removed at the source
    assert res.removed == ["f3"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_unreadable_items_are_kept_and_flagged_never_retired():
    admin, cid = uuid.uuid4(), uuid.uuid4()
    iid = await _seed(admin)
    await _sync(admin, iid, cid, Provider(_items(3)))
    # This pass could read f0 only; f1 and f2 timed out.
    res = await _sync(admin, iid, cid, Provider(_items(1), failed=["f1", "f2"]))
    assert res.removed == []
    live = await _live(admin)
    assert sorted(live) == ["f0", "f1", "f2"]
    assert live["f1"].sync_status == "error" and live["f2"].sync_status == "error"
    assert live["f0"].sync_status == "synced"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_clean_sync_clears_an_old_error_flag():
    admin, cid = uuid.uuid4(), uuid.uuid4()
    iid = await _seed(admin)
    await _sync(admin, iid, cid, Provider(_items(2)))
    async with tenant_session(admin) as s:
        await sync.mark_sync_error(s, admin_id=admin, connection_id=cid)
    assert {src.sync_status for src in (await _live(admin)).values()} == {"error"}
    await _sync(admin, iid, cid, Provider(_items(2)))  # unchanged content, healthy read
    assert {src.sync_status for src in (await _live(admin)).values()} == {"synced"}
