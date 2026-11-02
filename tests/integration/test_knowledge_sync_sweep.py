"""Scheduled knowledge sync (§3.2.3) — the periodic half of "live sync".

Before the ``knowledge_sync`` sweep, the sync engine was reachable only from the
two MANUAL dashboard routes: a "kept in sync" source was synced exactly as often
as the owner clicked, and a crawled website was never re-crawled. Pinned here:

  - candidate discovery finds ONLY tenants with a CONNECTED knowledge connection;
  - a fresh source (inside its origin's cadence) is NOT re-synced;
  - a stale source IS re-synced through the real sync engine;
  - a dead credential (ProviderUnavailable) pauses the sources — never a crash,
    never stale content silently served.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db.session import tenant_session
from app.jobs import sweeps
from app.knowledge import sweep as knowledge_sweep
from app.knowledge import sync
from app.knowledge.embeddings import EMBEDDING_DOWN, EmbeddingError
from app.knowledge.providers import CrawlUnreachable, ProviderUnavailable
from app.knowledge.sync import RemoteItem, RemoteSnapshot, SnapshotShrunk
from app.models.connection import InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.knowledge import KnowledgeSource
from sqlalchemy import select, text, update

pytestmark = [pytest.mark.asyncio]

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


async def _seed(
    *,
    status: str = "connected",
    last_synced_at: datetime | None = None,
    provider: str = "google_drive",
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
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
                provider=provider,
                status=status,
            )
        )
        s.add(
            KnowledgeSource(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                name="Playbook.pdf",
                origin=provider,
                source_origin=provider,
                external_id="drive-1",
                connection_id=cid,
                ingestion_status="ready",
                last_synced_at=last_synced_at,
                sync_status="synced",
            )
        )
    return admin, iid, cid


class _StableProvider:
    """Reports the existing item unchanged — a sync that verifies freshness."""

    origin = "google_drive"

    def __init__(self) -> None:
        self.calls = 0

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        self.calls += 1
        return RemoteSnapshot(
            items=[RemoteItem(external_id="drive-1", name="Playbook.pdf", text="v1", revision="r1")]
        )


class _DeadProvider:
    origin = "google_drive"

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        raise ProviderUnavailable("revoked")


async def test_discovery_finds_only_connected_knowledge_tenants() -> None:
    connected_admin, _, _ = await _seed(status="connected")
    expired_admin, _, _ = await _seed(status="expired")

    candidates = await sweeps.sweep_candidates(sweeps.KNOWLEDGE_SYNC)
    assert connected_admin in candidates
    assert expired_admin not in candidates


async def test_a_fresh_source_is_not_resynced(monkeypatch) -> None:
    admin, _, _ = await _seed(last_synced_at=NOW - timedelta(hours=1))
    provider = _StableProvider()
    monkeypatch.setattr(knowledge_sweep, "provider_for_connection", lambda conn, store: provider)

    async with tenant_session(admin) as s:
        synced = await knowledge_sweep.sync_due_connections(s, admin_id=admin, now=NOW)
    assert synced == 0
    assert provider.calls == 0  # inside the 6h delta cadence — no provider call


async def test_a_stale_source_is_resynced_through_the_real_engine(monkeypatch) -> None:
    admin, _, _ = await _seed(last_synced_at=NOW - timedelta(hours=12))
    provider = _StableProvider()
    monkeypatch.setattr(knowledge_sweep, "provider_for_connection", lambda conn, store: provider)

    async with tenant_session(admin) as s:
        synced = await knowledge_sweep.sync_due_connections(s, admin_id=admin, now=NOW)
    assert synced == 1
    assert provider.calls == 1
    async with tenant_session(admin) as s:
        src = (
            await s.execute(select(KnowledgeSource).where(KnowledgeSource.admin_id == admin))
        ).scalar_one()
        assert src.last_synced_at is not None
        assert src.last_synced_at.replace(tzinfo=UTC) >= NOW - timedelta(minutes=1)


async def test_a_dead_credential_pauses_the_sources_instead_of_crashing(monkeypatch) -> None:
    admin, _, _ = await _seed(last_synced_at=NOW - timedelta(days=2))
    monkeypatch.setattr(
        knowledge_sweep, "provider_for_connection", lambda conn, store: _DeadProvider()
    )

    async with tenant_session(admin) as s:
        synced = await knowledge_sweep.sync_due_connections(s, admin_id=admin, now=NOW)
    assert synced == 0
    async with tenant_session(admin) as s:
        src = (
            await s.execute(select(KnowledgeSource).where(KnowledgeSource.admin_id == admin))
        ).scalar_one()
        # §3.2.3: never silently serve stale content from a revoked credential.
        assert src.sync_status == "paused_reconnect_needed"


# --- Round 7 Phase 2 review, knowledge #2: an unreachable site is not a revoked sign-in -
# A crawl whose every page failed (a maintenance 503, a bot wall's 403) raised the same
# ProviderUnavailable as a revoked OAuth grant: the sources paused with the D10 7-day
# stop clock, the connection row flipped to error, and the owner was emailed that "its
# saved sign-in was revoked" on a connection that has no sign-in at all.


class _UnreachableSite:
    origin = "website_crawl"

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        raise CrawlUnreachable("crawl fetched no pages: https://acme.example/")


async def test_an_unreachable_site_is_a_failed_sync_not_a_revoked_credential(monkeypatch):
    from app.models.audit import AdminAuditLog
    from app.models.notice_delivery import NoticeDelivery

    admin, _, cid = await _seed(last_synced_at=NOW - timedelta(days=2), provider="website_crawl")
    monkeypatch.setattr(
        knowledge_sweep, "provider_for_connection", lambda conn, store: _UnreachableSite()
    )

    async with tenant_session(admin) as s:
        synced = await knowledge_sweep.sync_due_connections(s, admin_id=admin, now=NOW)
    assert synced == 0
    async with tenant_session(admin) as s:
        src = (
            await s.execute(select(KnowledgeSource).where(KnowledgeSource.admin_id == admin))
        ).scalar_one()
        # The honest failed-sync state — no pause, so no D10 stop clock.
        assert src.sync_status == "error"
        assert src.paused_since is None
        conn = (
            await s.execute(
                select(InstanceConnection).where(InstanceConnection.connection_id == cid)
            )
        ).scalar_one()
        assert conn.status == "connected"  # no credential to call broken
        # No "sign-in revoked" pause email, no connection attention flip.
        assert (
            await s.execute(select(NoticeDelivery).where(NoticeDelivery.admin_id == admin))
        ).first() is None
        events = [
            (r.event_type, r.event_payload)
            for r in (
                await s.execute(select(AdminAuditLog).where(AdminAuditLog.admin_id == admin))
            ).scalars()
        ]
        kinds = {e for e, _ in events}
        assert "knowledge_pause_notice_emailed" not in kinds
        assert "connection_status_changed" not in kinds
        failures = [p for e, p in events if p.get("action") == "sync_failed"]
        assert [f["error"] for f in failures] == ["CrawlUnreachable"]
        assert failures[0]["origin"] == "website_crawl"


# --- Round 7 WP-4c #3: a mid-sync failure leaves no "Processing…" ghost ---------------
# ingest_text flushes the KnowledgeSource row (status pending) BEFORE it embeds. When
# the embedder (or anything after that flush) failed, the sweep caught the exception
# inside the tenant transaction and then committed the flushed row: a chunkless
# pending source that read "Processing…" in the raw view forever.


class _GrowingProvider:
    """Reports the existing item unchanged plus one NEW page (so the sync ingests)."""

    origin = "google_drive"

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        return RemoteSnapshot(
            items=[
                RemoteItem(external_id="drive-1", name="Playbook.pdf", text="v1", revision="r1"),
                RemoteItem(
                    external_id="drive-2", name="Prices.pdf", text="prices here", revision="r1"
                ),
            ]
        )


class _EmbedderDown:
    model_name = "test-embedder"

    async def embed(self, texts):  # noqa: ANN001, ANN202 — Embedder protocol shape
        raise ConnectionError("embedding provider unreachable")


async def test_a_failure_after_the_source_row_is_flushed_leaves_no_pending_ghost(monkeypatch):
    from app.knowledge import service
    from app.models.audit import AdminAuditLog

    admin, _, cid = await _seed(last_synced_at=NOW - timedelta(hours=12))
    async with tenant_session(admin) as s:
        # The existing page is UNCHANGED at the source (revision matches), so the
        # only write this pass makes is the NEW page's row — the live shape.
        src = (
            await s.execute(select(KnowledgeSource).where(KnowledgeSource.admin_id == admin))
        ).scalar_one()
        src.original_object_key = "r1"
    monkeypatch.setattr(knowledge_sweep, "provider_for_connection", lambda c, s: _GrowingProvider())
    monkeypatch.setattr(service, "get_embedder", lambda: _EmbedderDown())

    async with tenant_session(admin) as s:
        synced = await knowledge_sweep.sync_due_connections(s, admin_id=admin, now=NOW)
    assert synced == 0
    async with tenant_session(admin) as s:
        rows = (
            (await s.execute(select(KnowledgeSource).where(KnowledgeSource.admin_id == admin)))
            .scalars()
            .all()
        )
        # The half-written "Prices.pdf" row (flushed pending, never embedded) is gone;
        # nothing reads pending.
        assert [r.name for r in rows] == ["Playbook.pdf"]
        assert all(r.ingestion_status != "pending" for r in rows)
        # The existing source carries the honest failure state, and the evidence row
        # names the failure class.
        assert rows[0].sync_status == "error"
        failures = [
            r.event_payload
            for r in (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "knowledge_source_edited",
                    )
                )
            ).scalars()
            if r.event_payload.get("action") == "sync_failed"
        ]
        assert failures and failures[0]["error"] == "ConnectionError"
        assert failures[0]["connection_id"] == str(cid)


async def test_the_failure_evidence_survives_the_sweep_transaction_unwinding(monkeypatch):
    """The bookkeeping is written in its OWN transaction: even when the tenant's
    sweep transaction is rolled back afterwards, the error state and the audit row
    are already durable (the shrink refusal has the same guarantee)."""
    from app.knowledge import service

    admin, _, _ = await _seed(last_synced_at=NOW - timedelta(hours=12))
    monkeypatch.setattr(knowledge_sweep, "provider_for_connection", lambda c, s: _GrowingProvider())
    monkeypatch.setattr(service, "get_embedder", lambda: _EmbedderDown())

    class _Unwind(Exception):
        pass

    with pytest.raises(_Unwind):
        async with tenant_session(admin) as s:
            await knowledge_sweep.sync_due_connections(s, admin_id=admin, now=NOW)
            raise _Unwind()  # the per-tenant runner's own failure path
    async with tenant_session(admin) as s:
        src = (
            await s.execute(select(KnowledgeSource).where(KnowledgeSource.admin_id == admin))
        ).scalar_one()
        assert src.sync_status == "error"


async def test_a_failing_connection_does_not_undo_an_earlier_connection_in_the_same_pass(
    monkeypatch,
):
    """The SAVEPOINT is per connection: the first connection's successful sync stays."""
    from app.knowledge import service

    admin, iid, first_cid = await _seed(last_synced_at=NOW - timedelta(hours=12))
    second_cid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=second_cid,
                admin_id=admin,
                instance_id=iid,
                connection_type="knowledge_source",
                auth_class="oauth_token",
                provider="notion",
                status="connected",
            )
        )

    calls: list[uuid.UUID] = []

    class _Embedder:
        model_name = "test-embedder"

        async def embed(self, texts):  # noqa: ANN001, ANN202
            if len(calls) > 1:
                raise ConnectionError("down for the second connection")
            return [[0.0] * 1536 for _ in texts]

    def _provider(conn, store):  # noqa: ANN001, ANN202
        calls.append(conn.connection_id)
        return _GrowingProvider()

    monkeypatch.setattr(knowledge_sweep, "provider_for_connection", _provider)
    monkeypatch.setattr(service, "get_embedder", lambda: _Embedder())

    async with tenant_session(admin) as s:
        synced = await knowledge_sweep.sync_due_connections(s, admin_id=admin, now=NOW)
    assert synced == 1 and calls == [first_cid, second_cid]
    async with tenant_session(admin) as s:
        by_conn: dict[uuid.UUID, list[str]] = {}
        for r in (
            (await s.execute(select(KnowledgeSource).where(KnowledgeSource.admin_id == admin)))
            .scalars()
            .all()
        ):
            by_conn.setdefault(r.connection_id, []).append(r.name)
        assert sorted(by_conn[first_cid]) == ["Playbook.pdf", "Prices.pdf"]  # kept
        assert second_cid not in by_conn  # unwound, no ghost


class _EmptyProvider:
    """A revoked scope / outage answering 200 with nothing — a shrink (F107)."""

    origin = "google_drive"

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        return RemoteSnapshot(items=[])


async def test_a_refused_shrink_records_its_evidence_exactly_once(monkeypatch) -> None:
    """The shrink refusal writes its own durable evidence before raising; the sweep's
    generic failure bookkeeping must not stack a second row on top of it."""
    from app.models.audit import AdminAuditLog

    admin, _, _ = await _seed(last_synced_at=NOW - timedelta(hours=12))
    monkeypatch.setattr(knowledge_sweep, "provider_for_connection", lambda c, s: _EmptyProvider())

    async with tenant_session(admin) as s:
        synced = await knowledge_sweep.sync_due_connections(s, admin_id=admin, now=NOW)
    assert synced == 0
    async with tenant_session(admin) as s:
        src = (
            await s.execute(select(KnowledgeSource).where(KnowledgeSource.admin_id == admin))
        ).scalar_one()
        assert src.sync_status == "error" and src.soft_deleted_at is None  # kept, flagged
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
        assert actions.count("sync_refused_shrink") == 1
        assert "sync_failed" not in actions


# --- Round 7 WP-4c #3b: the sweep honours NON_SERVING_STATES ---------------------------


@pytest.mark.parametrize(
    "state", [LucielState.paused, LucielState.luciel_grace_window, LucielState.luciel_hard_deleted]
)
async def test_a_non_serving_luciel_is_not_synced(monkeypatch, state: LucielState) -> None:
    admin, iid, _ = await _seed(last_synced_at=NOW - timedelta(days=2))
    async with tenant_session(admin) as s:
        inst = (await s.execute(select(Instance).where(Instance.id == iid))).scalar_one()
        inst.state = state
    provider = _StableProvider()
    monkeypatch.setattr(knowledge_sweep, "provider_for_connection", lambda conn, store: provider)

    async with tenant_session(admin) as s:
        synced = await knowledge_sweep.sync_due_connections(s, admin_id=admin, now=NOW)
    assert synced == 0
    assert provider.calls == 0  # never reached the provider


# --- Round 7 walk run 8: the scheduled sync stamps the same sync job -------------------
# The manual sync became a job whose state lives on the connection (sync_job). The
# sweep writes the same record with trigger "scheduled": "syncing" first, in its own
# transaction; the terminal stamp right after the tenant's commit. It leaves a connection
# alone while a job holds its lock (two reconciles would duplicate every new item).


async def _sync_job_of(admin: uuid.UUID, cid: uuid.UUID) -> dict | None:
    async with tenant_session(admin) as s:
        return (
            await s.execute(
                select(InstanceConnection.sync_job).where(InstanceConnection.connection_id == cid)
            )
        ).scalar_one()


async def _seed_job(admin: uuid.UUID, cid: uuid.UUID, record: dict | None) -> None:
    async with tenant_session(admin) as s:
        await s.execute(
            update(InstanceConnection)
            .where(InstanceConnection.connection_id == cid)
            .values(sync_job=record)
        )


class _Raises:
    origin = "google_drive"

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        raise self._exc


async def test_the_sweep_stamps_a_scheduled_job_synced_with_its_result(monkeypatch) -> None:
    admin, _, cid = await _seed(last_synced_at=NOW - timedelta(hours=12))
    provider = _StableProvider()
    monkeypatch.setattr(knowledge_sweep, "provider_for_connection", lambda conn, store: provider)

    assert str(admin) in await sweeps.run_knowledge_sync(now=NOW)
    job = await _sync_job_of(admin, cid)
    assert job is not None
    assert job["state"] == "synced"
    assert job["trigger"] == "scheduled"
    assert job["started_at"] and job["finished_at"]
    assert job["error"] is None
    assert job["result"]["added"] == [] and job["result"]["removed"] == []


async def test_syncing_is_visible_from_another_session_while_the_provider_runs(monkeypatch):
    admin, _, cid = await _seed(last_synced_at=NOW - timedelta(hours=12))
    seen: list[dict | None] = []

    class _Watching(_StableProvider):
        async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
            seen.append(await _sync_job_of(admin, cid))
            return await super().snapshot(connection_id)

    monkeypatch.setattr(knowledge_sweep, "provider_for_connection", lambda conn, store: _Watching())
    await sweeps.run_knowledge_sync(now=NOW)

    assert len(seen) == 1
    assert seen[0] is not None
    assert seen[0]["state"] == "syncing" and seen[0]["trigger"] == "scheduled"


@pytest.mark.parametrize(
    ("exc", "state", "error", "outcome"),
    [
        pytest.param(
            SnapshotShrunk(existing=4, remote=1),
            "shrink_pending",
            (
                "The source now reports 1 item(s) where 4 are synced, so nothing was removed. "
                "If you did remove them at the source, sync again and confirm the shrink."
            ),
            "shrink_pending",
            id="shrunk",
        ),
        pytest.param(
            ProviderUnavailable("revoked"),
            "failed",
            "Sync unavailable: revoked",
            "dead_credential",
            id="dead-credential",
        ),
        pytest.param(
            EmbeddingError("openai_error:RateLimitError"),
            "failed",
            EMBEDDING_DOWN,
            "embedding",
            id="embedding",
        ),
    ],
)
async def test_the_sweep_stamps_every_terminal_state(
    monkeypatch, exc: Exception, state: str, error: str, outcome: str
) -> None:
    from app.knowledge import sync_job

    admin, _, cid = await _seed(last_synced_at=NOW - timedelta(days=2))
    monkeypatch.setattr(knowledge_sweep, "provider_for_connection", lambda c, s: _Raises(exc))
    outcomes: list[str] = []
    real = sync_job.record_job_outcome

    def _recording(**kwargs: object) -> None:
        outcomes.append(str(kwargs["outcome"]))
        real(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sync_job, "record_job_outcome", _recording)
    await sweeps.run_knowledge_sync(now=NOW)

    job = await _sync_job_of(admin, cid)
    assert job is not None
    assert job["state"] == state and job["error"] == error and job["trigger"] == "scheduled"
    assert outcomes == [outcome]


async def test_the_sweep_stamps_a_failed_job_on_an_unreachable_site(monkeypatch) -> None:
    admin, _, cid = await _seed(last_synced_at=NOW - timedelta(days=2), provider="website_crawl")
    monkeypatch.setattr(
        knowledge_sweep, "provider_for_connection", lambda conn, store: _UnreachableSite()
    )

    await sweeps.run_knowledge_sync(now=NOW)
    job = await _sync_job_of(admin, cid)
    assert job is not None
    assert job["state"] == "failed"
    assert job["trigger"] == "scheduled"
    assert job["error"] == "Sync unavailable: crawl fetched no pages: https://acme.example/"


async def test_a_terminal_stamp_is_written_only_after_the_tenants_commit(monkeypatch) -> None:
    admin, _, cid = await _seed(last_synced_at=NOW - timedelta(hours=12))
    monkeypatch.setattr(
        knowledge_sweep, "provider_for_connection", lambda conn, store: _StableProvider()
    )
    pending: list[knowledge_sweep.PendingStamp] = []

    async with tenant_session(admin) as s:
        await knowledge_sweep.sync_due_connections(
            s, admin_id=admin, now=NOW, pending_stamps=pending
        )
        # The pass has not committed: its data is invisible and it holds no new row lock.
        assert (await _sync_job_of(admin, cid))["state"] == "syncing"
        async with tenant_session(admin) as other:
            await other.execute(text("SET LOCAL lock_timeout = '200ms'"))
            await other.execute(
                select(InstanceConnection)
                .where(InstanceConnection.connection_id == cid)
                .with_for_update(nowait=True)
            )
            # ... and the job is live: its lock is held until that commit.
            assert sync.sync_lock_k2(cid) in await sync.live_sync_lock_keys(other)
    assert (await _sync_job_of(admin, cid))["state"] == "syncing"
    assert [p.connection_id for p in pending] == [cid]

    await knowledge_sweep.write_pending_stamps(admin, pending)
    assert (await _sync_job_of(admin, cid))["state"] == "synced"


async def test_a_rolled_back_pass_writes_no_stamp_and_the_row_reads_interrupted(monkeypatch):
    from app.config import get_settings
    from app.knowledge import sync_job

    admin, _, cid = await _seed(last_synced_at=NOW - timedelta(hours=12))
    monkeypatch.setattr(
        knowledge_sweep, "provider_for_connection", lambda conn, store: _StableProvider()
    )

    async def _boom(*a: object, **k: object) -> None:
        raise RuntimeError("the pass failed after the sync")

    candidates = [admin]

    async def _candidates(kind: str) -> list[uuid.UUID]:
        return candidates

    async def _work(session, *, admin_id, now):  # noqa: ANN001, ANN202
        pending: list[knowledge_sweep.PendingStamp] = []
        await knowledge_sweep.sync_due_connections(
            session, admin_id=admin_id, now=now, pending_stamps=pending
        )
        await _boom()

    after_commit: list[uuid.UUID] = []

    async def _after(admin_id: uuid.UUID) -> None:
        after_commit.append(admin_id)

    monkeypatch.setattr(sweeps, "sweep_candidates", _candidates)
    assert await sweeps._per_tenant("test", _work, NOW, after_commit=_after) == []

    assert after_commit == []
    raw = await _sync_job_of(admin, cid)
    assert raw is not None and raw["state"] == "syncing"  # no terminal stamp was written
    monkeypatch.setattr(get_settings(), "knowledge_sync_job_start_grace_seconds", 0)
    async with tenant_session(admin) as s:
        conn = (
            await s.execute(
                select(InstanceConnection).where(InstanceConnection.connection_id == cid)
            )
        ).scalar_one()
        reading = sync_job.effective_job(conn, await sync_job.live_keys_for(s, [conn]))
    assert reading is not None and reading["state"] == "failed"
    assert reading["error"] == sync_job.INTERRUPTED_STALE


async def test_the_sweep_skips_a_connection_whose_lock_is_held(monkeypatch, caplog):
    import logging

    from app.knowledge import sync_job

    admin, _, cid = await _seed(last_synced_at=NOW - timedelta(days=2))
    # Past any age, and no fresh stamp either: only the lock says this job is alive.
    running = sync_job.syncing_stamp(
        started_at=datetime.now(UTC) - timedelta(days=1), trigger="manual"
    )
    await _seed_job(admin, cid, running)
    provider = _StableProvider()
    monkeypatch.setattr(knowledge_sweep, "provider_for_connection", lambda conn, store: provider)

    with caplog.at_level(logging.INFO, logger="app.knowledge.sweep"):
        async with tenant_session(admin) as holder:
            assert await sync.claim_sync_lock(holder, cid)
            await sweeps.run_knowledge_sync(now=NOW)
    assert provider.calls == 0  # left alone this pass
    assert await _sync_job_of(admin, cid) == running  # the job's record untouched
    assert any("knowledge_sync_skipped_job_running" in r.getMessage() for r in caplog.records)


async def test_the_sweep_skips_on_a_lock_it_cannot_take_and_stamps_nothing(monkeypatch, caplog):
    import logging

    admin, _, cid = await _seed(last_synced_at=NOW - timedelta(days=2))
    provider = _StableProvider()
    monkeypatch.setattr(knowledge_sweep, "provider_for_connection", lambda conn, store: provider)

    with caplog.at_level(logging.INFO, logger="app.knowledge.sweep"):
        async with tenant_session(admin) as holder:
            assert await sync.claim_sync_lock(holder, cid)
            await sweeps.run_knowledge_sync(now=NOW)
    assert provider.calls == 0
    assert await _sync_job_of(admin, cid) is None
    assert any("knowledge_sync_skipped_job_running" in r.getMessage() for r in caplog.records)


async def test_a_syncing_stamp_inside_the_start_grace_is_also_left_alone(monkeypatch):
    from app.knowledge import sync_job

    admin, _, cid = await _seed(last_synced_at=NOW - timedelta(days=2))
    running = sync_job.syncing_stamp(started_at=datetime.now(UTC), trigger="manual")
    await _seed_job(admin, cid, running)
    provider = _StableProvider()
    monkeypatch.setattr(knowledge_sweep, "provider_for_connection", lambda conn, store: provider)

    await sweeps.run_knowledge_sync(now=NOW)

    assert provider.calls == 0
    assert await _sync_job_of(admin, cid) == running


async def test_a_refused_syncing_stamp_skips_the_connection(monkeypatch, caplog):
    """The conditional stamp: a manual start committed between the sweep's read and its
    stamp, so the sweep's stamp matches nothing and the sweep does not run."""
    import logging

    from app.knowledge import sync_job

    admin, _, cid = await _seed(last_synced_at=NOW - timedelta(days=2))
    provider = _StableProvider()
    monkeypatch.setattr(knowledge_sweep, "provider_for_connection", lambda conn, store: provider)
    theirs = sync_job.syncing_stamp(started_at=datetime.now(UTC), trigger="manual")
    real = sync_job.stamp_syncing_if_unchanged

    async def _manual_start_first(*a: object, **k: object) -> bool:
        await _seed_job(admin, cid, theirs)
        return await real(*a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(sync_job, "stamp_syncing_if_unchanged", _manual_start_first)
    with caplog.at_level(logging.WARNING, logger="app.knowledge.sweep"):
        await sweeps.run_knowledge_sync(now=NOW)

    assert provider.calls == 0
    assert await _sync_job_of(admin, cid) == theirs
    assert any("knowledge_sync_skipped_stamp_refused" in r.getMessage() for r in caplog.records)


async def test_a_stamp_that_could_not_be_written_skips_the_connection(monkeypatch):
    admin, _, cid = await _seed(last_synced_at=NOW - timedelta(days=2))
    provider = _StableProvider()
    monkeypatch.setattr(knowledge_sweep, "provider_for_connection", lambda conn, store: provider)

    async def _false(*a: object, **k: object) -> bool:
        return False

    monkeypatch.setattr(knowledge_sweep.sync_jobs, "stamp_syncing_if_unchanged", _false)
    await sweeps.run_knowledge_sync(now=NOW)

    assert provider.calls == 0
    assert await _sync_job_of(admin, cid) is None


async def test_an_orphaned_syncing_record_does_not_block_the_sweep(monkeypatch) -> None:
    from app.knowledge import sync_job

    admin, _, cid = await _seed(last_synced_at=NOW - timedelta(days=2))
    orphan = sync_job.syncing_stamp(
        started_at=datetime.now(UTC) - timedelta(hours=2), trigger="manual"
    )
    await _seed_job(admin, cid, orphan)
    provider = _StableProvider()
    monkeypatch.setattr(knowledge_sweep, "provider_for_connection", lambda conn, store: provider)

    await sweeps.run_knowledge_sync(now=NOW)
    assert provider.calls == 1
    job = await _sync_job_of(admin, cid)
    assert job is not None and job["state"] == "synced" and job["trigger"] == "scheduled"


async def test_a_terminal_stamp_leaves_a_record_it_does_not_own(monkeypatch) -> None:
    from app.knowledge import sync_job

    admin, _, cid = await _seed(last_synced_at=NOW - timedelta(days=2))
    newer = sync_job.syncing_stamp(
        started_at=datetime.now(UTC) + timedelta(seconds=30), trigger="manual"
    )

    class _Superseded(_StableProvider):
        async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
            await _seed_job(admin, cid, newer)
            return await super().snapshot(connection_id)

    monkeypatch.setattr(knowledge_sweep, "provider_for_connection", lambda c, s: _Superseded())
    await sweeps.run_knowledge_sync(now=NOW)

    assert await _sync_job_of(admin, cid) == newer


async def test_removing_the_source_mid_pass_keeps_nothing_and_leaves_no_record(monkeypatch):
    admin, _, cid = await _seed(last_synced_at=NOW - timedelta(days=2))

    class _Removed(_StableProvider):
        async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
            async with tenant_session(admin) as s:
                await s.execute(
                    update(InstanceConnection)
                    .where(InstanceConnection.connection_id == cid)
                    .values(status="not_connected", sync_job=None)
                )
            snapshot = await super().snapshot(connection_id)
            snapshot.items.append(
                RemoteItem(external_id="drive-2", name="New.pdf", text="new", revision="r1")
            )
            return snapshot

    monkeypatch.setattr(knowledge_sweep, "provider_for_connection", lambda c, s: _Removed())
    await sweeps.run_knowledge_sync(now=NOW)

    assert await _sync_job_of(admin, cid) is None
    async with tenant_session(admin) as s:
        names = (
            (await s.execute(select(KnowledgeSource.name).where(KnowledgeSource.admin_id == admin)))
            .scalars()
            .all()
        )
    assert list(names) == ["Playbook.pdf"]
