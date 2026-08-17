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
