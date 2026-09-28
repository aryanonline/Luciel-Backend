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
from app.knowledge.providers import ProviderUnavailable
from app.knowledge.sync import RemoteItem, RemoteSnapshot
from app.models.connection import InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.knowledge import KnowledgeSource
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


async def _seed(
    *, status: str = "connected", last_synced_at: datetime | None = None
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
                provider="google_drive",
                status=status,
            )
        )
        s.add(
            KnowledgeSource(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                name="Playbook.pdf",
                origin="google_drive",
                source_origin="google_drive",
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
