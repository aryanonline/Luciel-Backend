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
from sqlalchemy import select


class FakeDriveProvider:
    origin = "google_drive"

    def __init__(self, items: list[RemoteItem]) -> None:
        self._items = items
        self.complete = True
        self.unreached_ids: list[str] = []

    def set_items(self, items: list[RemoteItem]) -> None:
        self._items = items

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        return RemoteSnapshot(
            items=list(self._items),
            unreached_ids=list(self.unreached_ids),
            complete=self.complete,
        )


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


# --- Round 7 WP-4c #2: sync updates go through the same quota / size / graph path -----
# The update branch used to re-chunk + re-embed inline: no PER_FILE_MAX_BYTES or
# TOTAL_QUOTA_BYTES check, size_bytes frozen at the first sync, the graph never
# re-extracted. A synced page could grow past every limit and the raw view would
# still say "synced" with the size it had on day one.


async def _source_rows(admin: uuid.UUID) -> dict[str, object]:
    async with tenant_session(admin) as s:
        rows = await service.list_sources(s, admin)
        used = await service.used_bytes(s, admin)
        return {"rows": rows, "used": used}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_synced_page_that_grows_past_the_per_file_cap_is_refused_honestly(monkeypatch):
    monkeypatch.setattr(service, "PER_FILE_MAX_BYTES", 60)
    admin = uuid.uuid4()
    iid = await _seed(admin)
    conn = uuid.uuid4()
    small = "opening hours weekdays nine to five"  # 35 bytes
    provider = FakeDriveProvider(
        [RemoteItem(external_id="page", name="Hours", text=small, revision="r1")]
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
    assert res.added == ["page"] and res.refused == []

    # The page grows past the cap at the source.
    grown = small + " and saturdays by appointment only, closed on public holidays"
    assert len(grown.encode()) > 60
    provider.set_items([RemoteItem(external_id="page", name="Hours", text=grown, revision="r2")])
    async with tenant_session(admin) as s:
        res = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=conn,
            origin="google_drive",
            provider=provider,
        )
    assert res.updated == [] and res.refused == ["page"]
    state = await _source_rows(admin)
    src = state["rows"][0]
    # Honest state: refused, previous revision still serving, size unchanged.
    assert src.sync_status == "error"
    assert src.original_object_key == "r1"  # revision NOT bumped: the next pass retries
    assert src.size_bytes == len(small.encode())
    assert state["used"] == len(small.encode())
    async with tenant_session(admin) as s:
        chunks = await service.get_chunks(s, admin, src.id)
        assert chunks and "saturdays" not in " ".join(c.text for c in chunks)

    # It shrinks back under the cap: the update goes through and the size follows.
    trimmed = small + " (call ahead)"
    provider.set_items([RemoteItem(external_id="page", name="Hours", text=trimmed, revision="r3")])
    async with tenant_session(admin) as s:
        res = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=conn,
            origin="google_drive",
            provider=provider,
        )
    assert res.updated == ["page"] and res.refused == []
    state = await _source_rows(admin)
    src = state["rows"][0]
    assert src.sync_status == "synced" and src.original_object_key == "r3"
    assert src.size_bytes == len(trimmed.encode())
    assert state["used"] == len(trimmed.encode())  # used_bytes reflects the new size


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_synced_update_carries_size_and_graph_forward():
    from app.models.audit import AdminAuditLog
    from app.models.knowledge import KnowledgeEntity

    admin = uuid.uuid4()
    iid = await _seed(admin)
    conn = uuid.uuid4()
    v1 = "Dr. Alice Smith (Dentist)"
    provider = FakeDriveProvider(
        [RemoteItem(external_id="team", name="Team", text=v1, revision="r1")]
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
    v2 = "Dr. Alice Smith (Dentist)\nBob Jones (Hygienist)\nBob Jones -WORKS_WITH-> Dr. Alice Smith"
    provider.set_items([RemoteItem(external_id="team", name="Team", text=v2, revision="r2")])
    async with tenant_session(admin) as s:
        res = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=conn,
            origin="google_drive",
            provider=provider,
        )
    assert res.updated == ["team"]
    async with tenant_session(admin) as s:
        src = (await service.list_sources(s, admin))[0]
        assert src.size_bytes == len(v2.encode())
        assert await service.used_bytes(s, admin) == len(v2.encode())
        names = sorted(
            (
                await s.execute(
                    select(KnowledgeEntity.canonical).where(KnowledgeEntity.source_id == src.id)
                )
            ).scalars()
        )
        assert names == ["bob jones", "dr. alice smith"]  # the graph followed the page
        actions = [
            r.event_payload.get("action")
            for r in (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "knowledge_source_edited",
                    )
                )
            ).scalars()
        ]
        assert "sync_update" in actions


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_new_synced_item_over_the_cap_is_not_ingested(monkeypatch):
    monkeypatch.setattr(service, "PER_FILE_MAX_BYTES", 20)
    admin = uuid.uuid4()
    iid = await _seed(admin)
    conn = uuid.uuid4()
    provider = FakeDriveProvider(
        [
            RemoteItem(external_id="ok", name="Small", text="tiny page", revision="r1"),
            RemoteItem(external_id="big", name="Big", text="x" * 100, revision="r1"),
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
    assert res.added == ["ok"] and res.refused == ["big"]
    # The refusal travels with the result the manual route answers (knowledge #4).
    assert res.as_dict()["refused"] == ["big"]
    async with tenant_session(admin) as s:
        rows = await service.list_sources(s, admin)
        assert [r.external_id for r in rows] == ["ok"]  # no half-written row for "big"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_origin_removal_is_not_restorable_and_is_purged_without_the_window():
    """Round 7 WP-4c #9: an item gone at its sync origin used to be tombstoned as an
    OWNER delete — restorable for 30 days (reviving a page Drive no longer had) and
    held for the whole window before the purge. It is now an ``origin`` tombstone:
    restore refuses it honestly, the next retention pass purges it, and the owner's
    own tombstone made at the same moment keeps its window."""
    from datetime import UTC, datetime

    from app.models.audit import AdminAuditLog
    from app.models.knowledge import KnowledgeChunk, KnowledgeSource
    from sqlalchemy import func

    admin = uuid.uuid4()
    iid = await _seed(admin)
    conn = uuid.uuid4()
    doc1 = RemoteItem(
        external_id="doc1", name="Playbook A", text="alpha pricing 899", revision="r1"
    )
    doc2 = RemoteItem(
        external_id="doc2", name="Playbook B", text="beta hours weekdays", revision="r1"
    )
    provider = FakeDriveProvider([doc1, doc2])
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
        # The owner's own delete, made in the same moment, keeps its 30-day window.
        mine = await service.ingest_text(
            s, admin_id=admin, instance_id=iid, name="mine.txt", text="owner deleted this by hand"
        )
        mine_id = mine.id
        assert await service.delete_source(s, admin, mine_id, admin) is True

    # doc2 disappears at the origin.
    provider.set_items([doc1])
    async with tenant_session(admin) as s:
        res = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=conn,
            origin="google_drive",
            provider=provider,
        )
        assert res.removed == ["doc2"]

    async with tenant_session(admin) as s:
        gone = (
            await s.execute(
                select(KnowledgeSource).where(
                    KnowledgeSource.admin_id == admin, KnowledgeSource.external_id == "doc2"
                )
            )
        ).scalar_one()
        gone_id = gone.id
        assert gone.soft_deleted_at is not None
        assert gone.deleted_by == service.DELETED_BY_ORIGIN
        # The audit row says who tombstoned it and advertises no window.
        by_source = {
            r.event_payload["source_id"]: r.event_payload
            for r in (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "knowledge_source_deleted",
                    )
                )
            ).scalars()
        }
        assert by_source[str(gone_id)]["deleted_by"] == "origin"
        assert by_source[str(gone_id)]["grace_days"] == 0
        assert by_source[str(mine_id)]["deleted_by"] == "owner"
        assert by_source[str(mine_id)]["grace_days"] == service.DELETE_GRACE_DAYS
        # Restore refuses it — as an origin removal, not "not in its undo window".
        with pytest.raises(service.RemovedAtOrigin):
            await service.restore_source(s, admin, gone_id, admin)

    # The next retention pass purges it NOW — no 30 days — while the owner tombstone
    # of the same age keeps its window.
    async with tenant_session(admin) as s:
        assert await service.purge_expired_deletions(s, admin_id=admin, now=datetime.now(UTC)) == 1
    async with tenant_session(admin) as s:
        assert (
            await s.execute(select(KnowledgeSource).where(KnowledgeSource.id == gone_id))
        ).scalar_one_or_none() is None
        assert (
            await s.execute(
                select(func.count())
                .select_from(KnowledgeChunk)
                .where(KnowledgeChunk.source_id == gone_id)
            )
        ).scalar_one() == 0
        kept = (
            await s.execute(select(KnowledgeSource).where(KnowledgeSource.id == mine_id))
        ).scalar_one()
        assert kept.soft_deleted_at is not None and kept.deleted_by == service.DELETED_BY_OWNER
        assert await service.restore_source(s, admin, mine_id, admin) is not None
        # A second pass has nothing left to purge.
        assert await service.purge_expired_deletions(s, admin_id=admin, now=datetime.now(UTC)) == 0

    # The honest way back: re-add it at the origin and sync — a fresh source.
    provider.set_items([doc1, doc2])
    async with tenant_session(admin) as s:
        res = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=conn,
            origin="google_drive",
            provider=provider,
        )
        assert res.added == ["doc2"] and res.removed == []
    async with tenant_session(admin) as s:
        ext_ids = {x.external_id for x in await service.list_sources(s, admin)}
        assert ext_ids == {"doc1", "doc2", None}


# --- Round 7 Phase 2 review, knowledge #1: a sync never spends the customers' share ----
# Every ingested page's LLM graph call is admitted under the tenant's per-minute cap —
# the SAME counter its customer turns are admitted under. A large sync used to spend
# the whole minute, and a customer on the widget got the at-cap "unavailable" reply
# because of a background job. Above half the cap the pass is skipped (grammar only),
# and the skipped pages keep an unbumped revision so the next pass re-extracts them.

_GRAPH_REPLY = '{"entities":[{"name":"Lifetime Warranty","type":"Policy"}],"edges":[]}'


def _page(i: int, rev: str = "r1") -> RemoteItem:
    return RemoteItem(
        external_id=f"page{i}",
        name=f"Page {i}",
        text=f"Page {i}. Our widgets ship in two days and every one carries a lifetime "
        "warranty, honoured at any of our stores.",
        revision=rev,
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_large_sync_leaves_the_customers_their_share_of_the_llm_cap(monkeypatch):
    import app.runtime.llm_cap as llm_cap
    from app.knowledge import graph_extract as ge
    from app.models.knowledge import KnowledgeEntity, KnowledgeSource
    from app.runtime.llm_router import LLMRouter
    from app.runtime.testing import EchoModel

    store = llm_cap.InMemoryLLMCapStore()
    monkeypatch.setattr(llm_cap, "_store", store)
    # The production shape: the extractor's router is built WITHOUT a tenant and never
    # opens a turn, so LLMRouter.complete admits every call under the ingesting tenant.
    monkeypatch.setattr(
        ge,
        "_default",
        ge.LlmGraphExtractor(
            router_factory=lambda: LLMRouter(
                primary=EchoModel(fixed_reply=_GRAPH_REPLY),
                fallback=EchoModel(fixed_reply=_GRAPH_REPLY),
            )
        ),
    )
    admin = uuid.uuid4()
    iid = await _seed(admin)
    conn = uuid.uuid4()
    cap = llm_cap.LLM_CAP_PER_MINUTE
    headroom = int(cap * ge.INGEST_HEADROOM_FRACTION)
    pages = [_page(i) for i in range(cap)]
    assert all(len(p.text) >= ge.MIN_LLM_TEXT_CHARS for p in pages)
    provider = FakeDriveProvider(pages)

    async with tenant_session(admin) as s:
        res = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=conn,
            origin="google_drive",
            provider=provider,
        )
    # Every page was ingested and serves; only half the cap went to the graph pass.
    assert len(res.added) == cap
    assert store.current_count(str(admin)) == headroom
    assert len(res.graph_deferred) == cap - headroom
    # A customer turn right after the sync is still admitted (it used to be refused:
    # the sync alone drove the counter to the cap).
    async with LLMRouter(EchoModel(), EchoModel(), admin_id=admin).turn():
        pass

    async def _rows() -> dict[str, tuple[str | None, set[str]]]:
        async with tenant_session(admin) as s:
            out: dict[str, tuple[str | None, set[str]]] = {}
            for src in (
                await s.execute(
                    select(KnowledgeSource).where(
                        KnowledgeSource.admin_id == admin,
                        KnowledgeSource.soft_deleted_at.is_(None),
                    )
                )
            ).scalars():
                types = set(
                    (
                        await s.execute(
                            select(KnowledgeEntity.entity_type).where(
                                KnowledgeEntity.source_id == src.id
                            )
                        )
                    ).scalars()
                )
                out[str(src.external_id)] = (src.original_object_key, types)
            return out

    rows = await _rows()
    deferred = set(res.graph_deferred)
    for ext_id, (revision, types) in rows.items():
        if ext_id in deferred:
            assert revision is None and "Policy" not in types  # grammar only, marker unset
        else:
            assert revision == "r1" and "Policy" in types  # the LLM pass ran

    # The next pass (a fresh minute) re-extracts exactly the deferred pages — nothing
    # changed at the source, so nothing else is touched.
    monkeypatch.setattr(llm_cap, "_store", llm_cap.InMemoryLLMCapStore())
    async with tenant_session(admin) as s:
        res2 = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=conn,
            origin="google_drive",
            provider=provider,
        )
    assert set(res2.updated) == deferred and res2.added == [] and res2.graph_deferred == []
    assert all(rev == "r1" and "Policy" in types for rev, types in (await _rows()).values())


# --- Round 7 Phase 2 review, knowledge #3: an incomplete snapshot never retires a page -
# A crawl page that merely fell outside the run's page/depth budget was absent from a
# snapshot that claimed to be complete: tombstoned "removed at origin", dropped from
# retrieval at once, hard-deleted by that night's retention pass, and Restore told the
# owner it was removed from their site.


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_page_an_incomplete_snapshot_did_not_reach_is_kept_and_flagged():
    from datetime import UTC, datetime

    from app.models.audit import AdminAuditLog
    from app.models.knowledge import KnowledgeChunk, KnowledgeSource

    admin = uuid.uuid4()
    iid = await _seed(admin)
    conn = uuid.uuid4()
    doc1 = RemoteItem(external_id="doc1", name="Home", text="alpha pricing 899", revision="r1")
    doc2 = RemoteItem(external_id="doc2", name="Pricing", text="beta hours weekdays", revision="r1")
    provider = FakeDriveProvider([doc1, doc2])
    async with tenant_session(admin) as s:
        res = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=conn,
            origin="website_crawl",
            provider=provider,
        )
        assert set(res.added) == {"doc1", "doc2"}

    # The budget ended the walk before doc2 this time.
    provider.set_items([doc1])
    provider.complete = False
    async with tenant_session(admin) as s:
        res = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=conn,
            origin="website_crawl",
            provider=provider,
        )
        assert res.removed == []

    async def _doc2() -> KnowledgeSource:
        async with tenant_session(admin) as s:
            return (
                await s.execute(
                    select(KnowledgeSource).where(
                        KnowledgeSource.admin_id == admin, KnowledgeSource.external_id == "doc2"
                    )
                )
            ).scalar_one()

    kept = await _doc2()
    assert kept.soft_deleted_at is None and kept.deleted_by is None
    assert kept.sync_status == "error"  # honest: not verified this pass (F117 semantics)
    async with tenant_session(admin) as s:
        # Nothing for the retention pass to purge; the page still has its chunks.
        assert await service.purge_expired_deletions(s, admin_id=admin, now=datetime.now(UTC)) == 0
        assert (
            await s.execute(select(KnowledgeChunk).where(KnowledgeChunk.source_id == kept.id))
        ).first() is not None
        payloads = [
            r.event_payload
            for r in (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "knowledge_source_edited",
                    )
                )
            ).scalars()
            if r.event_payload.get("action") == "sync"
        ]
        assert ["doc2"] in [p.get("unreached") for p in payloads]

    # Reached again on a later pass: synced, nothing was lost or re-ingested.
    provider.set_items([doc1, doc2])
    async with tenant_session(admin) as s:
        res = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=conn,
            origin="website_crawl",
            provider=provider,
        )
        assert res.added == [] and res.updated == [] and res.removed == []
    back = await _doc2()
    assert back.id == kept.id and back.sync_status == "synced"

    # A COMPLETE snapshot without doc2 is still a removal at the origin (the existing path).
    provider.set_items([doc1])
    provider.complete = True
    async with tenant_session(admin) as s:
        res = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=conn,
            origin="website_crawl",
            provider=provider,
        )
        assert res.removed == ["doc2"]
    gone = await _doc2()
    assert gone.soft_deleted_at is not None and gone.deleted_by == service.DELETED_BY_ORIGIN


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_incomplete_snapshot_is_not_a_shrink():
    """Keeping everything it did not reach, an incomplete snapshot retires nothing, so the
    F107 floor (which guards against a mass retirement) has nothing to refuse."""
    admin = uuid.uuid4()
    iid = await _seed(admin)
    conn = uuid.uuid4()
    pages = [
        RemoteItem(external_id=f"p{i}", name=f"P{i}", text=f"page {i} text", revision="r1")
        for i in range(4)
    ]
    provider = FakeDriveProvider(pages)
    async with tenant_session(admin) as s:
        await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=conn,
            origin="website_crawl",
            provider=provider,
        )
    provider.set_items(pages[:1])
    provider.complete = False
    async with tenant_session(admin) as s:
        res = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=conn,
            origin="website_crawl",
            provider=provider,
        )
    assert res.removed == []
    async with tenant_session(admin) as s:
        rows = {r.external_id: r.sync_status for r in await service.list_sources(s, admin)}
    assert rows == {"p0": "synced", "p1": "error", "p2": "error", "p3": "error"}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_page_known_beyond_the_budget_is_kept_and_an_unlinked_one_retires():
    """A complete walk that names a page it knows of but did not visit (linked from the
    depth cap) keeps that page; a synced page the complete walk neither reached nor
    heard of is gone from the site's reach and still retires as removed at origin."""
    from app.models.knowledge import KnowledgeSource

    admin = uuid.uuid4()
    iid = await _seed(admin)
    conn = uuid.uuid4()
    docs = [
        RemoteItem(external_id=f"d{i}", name=f"D{i}", text=f"page {i} words", revision="r1")
        for i in range(3)
    ]
    provider = FakeDriveProvider(docs)
    async with tenant_session(admin) as s:
        await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=conn,
            origin="website_crawl",
            provider=provider,
        )
    provider.set_items(docs[:1])
    provider.unreached_ids = ["d1"]
    async with tenant_session(admin) as s:
        res = await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=conn,
            origin="website_crawl",
            provider=provider,
        )
    assert res.removed == ["d2"]
    async with tenant_session(admin) as s:
        rows = {
            r.external_id: r
            for r in (
                await s.execute(select(KnowledgeSource).where(KnowledgeSource.admin_id == admin))
            ).scalars()
        }
    assert rows["d1"].soft_deleted_at is None and rows["d1"].sync_status == "error"
    assert rows["d2"].deleted_by == service.DELETED_BY_ORIGIN
