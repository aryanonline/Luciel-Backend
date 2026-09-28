"""Owner decision D10 (2026-09-14; round 7 WP-4c #5): a knowledge source whose sync
was paused by a revoked/expired credential keeps answering for seven days, then
stops until the owner reconnects — and the owner is told on day 1 and on day 7.

Before this, ``service.retrieve`` filtered on tenant and tombstone only, so a
revoked Drive folder kept answering forever with content nobody was keeping true.
Pinned here:

  - retrieval AND graph traversal serve a paused source inside the window and
    exclude it at the 7-day boundary;
  - the clock starts ONCE per incident (a second pause keeps the stop date) and a
    clean sync clears it;
  - the knowledge_sync sweep emails the owner once on day 1 (naming the sources and
    the stop date) and once on day 7, never twice, with the ledger as proof;
  - a dead credential the sweep itself discovers starts the clock and warns in the
    same pass.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db.session import tenant_session
from app.knowledge import graph, service, sync
from app.knowledge import sweep as knowledge_sweep
from app.knowledge.providers import ProviderUnavailable
from app.knowledge.sync import RemoteItem, RemoteSnapshot
from app.models.audit import AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.knowledge import KnowledgeSource
from app.models.notice_delivery import NoticeDelivery
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
GRAPH_TEXT = """
Platform (Department)
Senior Engineer (Role): level=senior
Python (Skill)
Platform -HAS-> Senior Engineer
Senior Engineer -REQUIRES-> Python
"""


class _CapturingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, *, to: str, subject: str, body_text: str, tags=None) -> str:  # noqa: ANN001
        self.sent.append((to, subject, body_text))
        return f"msg-{len(self.sent)}"


async def _seed(
    *, connection_status: str = "connected", names: tuple[str, ...] = ("Roles.txt",)
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, list[uuid.UUID]]:
    """A verified owner, an active Luciel, one Drive connection and its synced sources."""
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
                status=connection_status,
            )
        )
        await s.flush()
        source_ids: list[uuid.UUID] = []
        for i, name in enumerate(names):
            src = await service.ingest_text(
                s,
                admin_id=admin,
                instance_id=iid,
                name=name,
                text=GRAPH_TEXT,
                origin="google_drive",
            )
            src.connection_id = cid
            src.external_id = f"drive-{i}"
            src.original_object_key = "r1"
            src.sync_status = "synced"
            src.last_synced_at = NOW - timedelta(days=1)
            source_ids.append(src.id)
    return admin, iid, cid, source_ids


async def _source(admin: uuid.UUID, sid: uuid.UUID) -> KnowledgeSource:
    async with tenant_session(admin) as s:
        return (
            await s.execute(select(KnowledgeSource).where(KnowledgeSource.id == sid))
        ).scalar_one()


async def _serving(admin: uuid.UUID, iid: uuid.UUID) -> tuple[bool, bool]:
    """(vector retrieval finds it, graph traversal walks it)."""
    async with tenant_session(admin) as s:
        hits = await service.retrieve(
            s, admin_id=admin, instance_id=iid, query="which roles require Python", k=5
        )
        facts = await graph.graph_facts_for_terms(
            s, admin_id=admin, instance_id=iid, terms=["Platform"]
        )
        return bool(hits), bool(facts)


async def _notice_events(admin: uuid.UUID) -> list[str]:
    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog)
                    .where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "knowledge_pause_notice_emailed",
                    )
                    .order_by(AdminAuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )
        return [str(r.event_payload["notice_type"]) for r in rows]


# --- serving boundary -------------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_paused_source_answers_inside_seven_days_and_stops_at_the_boundary():
    admin, iid, cid, (sid,) = await _seed()
    assert await _serving(admin, iid) == (True, True)

    async with tenant_session(admin) as s:
        assert await sync.pause_sync_on_revocation(s, admin_id=admin, connection_id=cid) == 1
    src = await _source(admin, sid)
    assert src.sync_status == "paused_reconnect_needed" and src.paused_since is not None
    # Day 1 of the incident: still answering (D10 — the owner has a week to reconnect).
    assert await _serving(admin, iid) == (True, True)

    real_now = datetime.now(UTC)
    async with tenant_session(admin) as s:
        src = (
            await s.execute(select(KnowledgeSource).where(KnowledgeSource.id == sid))
        ).scalar_one()
        src.paused_since = real_now - timedelta(days=6, hours=23)
    assert await _serving(admin, iid) == (True, True)  # an hour inside the window

    async with tenant_session(admin) as s:
        src = (
            await s.execute(select(KnowledgeSource).where(KnowledgeSource.id == sid))
        ).scalar_one()
        src.paused_since = real_now - timedelta(days=7, minutes=1)
    # Past the boundary: neither the chunks nor the graph answer any more.
    assert await _serving(admin, iid) == (False, False)
    # The row is untouched otherwise — the raw view still lists it, honestly paused.
    src = await _source(admin, sid)
    assert src.soft_deleted_at is None and src.ingestion_status == "ready"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_clock_starts_once_per_incident_and_a_clean_sync_clears_it():
    admin, iid, cid, (sid,) = await _seed()
    async with tenant_session(admin) as s:
        await sync.pause_sync_on_revocation(s, admin_id=admin, connection_id=cid, now=NOW)
    async with tenant_session(admin) as s:
        # The health sweep and a manual re-sync both pause; the second must not push
        # the stop date out.
        await sync.pause_sync_on_revocation(
            s, admin_id=admin, connection_id=cid, now=NOW + timedelta(days=2)
        )
    assert (await _source(admin, sid)).paused_since == NOW

    class _Provider:
        origin = "google_drive"

        async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
            return RemoteSnapshot(
                items=[
                    RemoteItem(
                        external_id="drive-0", name="Roles.txt", text=GRAPH_TEXT, revision="r1"
                    )
                ]
            )

    async with tenant_session(admin) as s:
        await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=cid,
            origin="google_drive",
            provider=_Provider(),
        )
    src = await _source(admin, sid)
    assert src.paused_since is None and src.sync_status == "synced"


# --- the two notices ----------------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_sweep_warns_on_day_one_and_notifies_on_day_seven_once_each():
    # The connection is already flagged broken (the health sweep did that), so the
    # sync loop skips it; the notice pass is what this pins.
    admin, _iid, cid, (sid,) = await _seed(connection_status="error")
    async with tenant_session(admin) as s:
        await sync.pause_sync_on_revocation(
            s, admin_id=admin, connection_id=cid, now=NOW - timedelta(hours=1)
        )

    async with tenant_session(admin) as s:
        await knowledge_sweep.sync_due_connections(s, admin_id=admin, now=NOW)
    assert await _notice_events(admin) == ["knowledge_pause_warning"]

    # Later passes inside the window: nothing new — once per incident.
    for later in (NOW + timedelta(hours=6), NOW + timedelta(days=3)):
        async with tenant_session(admin) as s:
            await knowledge_sweep.sync_due_connections(s, admin_id=admin, now=later)
    assert await _notice_events(admin) == ["knowledge_pause_warning"]

    # The boundary: the stop notice, exactly once.
    for later in (NOW + timedelta(days=7), NOW + timedelta(days=8)):
        async with tenant_session(admin) as s:
            await knowledge_sweep.sync_due_connections(s, admin_id=admin, now=later)
    assert await _notice_events(admin) == ["knowledge_pause_warning", "knowledge_pause_stopped"]

    async with tenant_session(admin) as s:
        ledger = (
            (await s.execute(select(NoticeDelivery).where(NoticeDelivery.admin_id == admin)))
            .scalars()
            .all()
        )
        assert sorted(r.kind for r in ledger) == [
            "knowledge_pause_stopped",
            "knowledge_pause_warning",
        ]
        assert all(r.notice_id.startswith(f"kbpause:{cid}:") for r in ledger)
    # Still paused, still honest: the clock was never touched by the notices.
    src = await _source(admin, sid)
    assert src.sync_status == "paused_reconnect_needed"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_warning_names_the_sources_and_the_stop_date_and_the_stop_notice_follows():
    admin, _iid, cid, _ = await _seed(
        connection_status="error", names=("Pricing.pdf", "Opening hours.gdoc")
    )
    async with tenant_session(admin) as s:
        await sync.pause_sync_on_revocation(s, admin_id=admin, connection_id=cid, now=NOW)

    sender = _CapturingSender()
    async with tenant_session(admin) as s:
        assert (
            await knowledge_sweep.notify_paused_sources(s, admin_id=admin, now=NOW, sender=sender)
            == 1
        )
    to, subject, body = sender.sent[0]
    assert to == f"{admin}@x.com"
    # One email for the connection, naming the provider, every source and the date.
    assert "Google Drive" in subject and "27 September 2026" in subject
    assert "Pricing.pdf" in body and "Opening hours.gdoc" in body
    assert "27 September 2026" in body and "Reconnect" in body

    async with tenant_session(admin) as s:
        assert (
            await knowledge_sweep.notify_paused_sources(
                s, admin_id=admin, now=NOW + timedelta(days=1), sender=sender
            )
            == 0
        )
    assert len(sender.sent) == 1

    async with tenant_session(admin) as s:
        assert (
            await knowledge_sweep.notify_paused_sources(
                s, admin_id=admin, now=NOW + timedelta(days=7), sender=sender
            )
            == 1
        )
    _to, subject, body = sender.sent[1]
    assert "stopped answering" in subject and "Google Drive" in subject
    assert "Pricing.pdf" in body and "27 September 2026" in body


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_incident_first_seen_after_its_stop_date_gets_only_the_stop_notice():
    """A warning about a date already past would be a lie."""
    admin, _iid, cid, _ = await _seed(connection_status="error")
    async with tenant_session(admin) as s:
        await sync.pause_sync_on_revocation(
            s, admin_id=admin, connection_id=cid, now=NOW - timedelta(days=9)
        )
    async with tenant_session(admin) as s:
        await knowledge_sweep.sync_due_connections(s, admin_id=admin, now=NOW)
    assert await _notice_events(admin) == ["knowledge_pause_stopped"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_dead_credential_found_by_the_sweep_starts_the_clock_and_warns_at_once(
    monkeypatch,
):
    admin, _iid, cid, (sid,) = await _seed()
    async with tenant_session(admin) as s:
        src = (
            await s.execute(select(KnowledgeSource).where(KnowledgeSource.id == sid))
        ).scalar_one()
        src.last_synced_at = NOW - timedelta(days=2)  # due

    class _Dead:
        origin = "google_drive"

        async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
            raise ProviderUnavailable("revoked")

    monkeypatch.setattr(knowledge_sweep, "provider_for_connection", lambda c, s: _Dead())
    async with tenant_session(admin) as s:
        await knowledge_sweep.sync_due_connections(s, admin_id=admin, now=NOW)
    src = await _source(admin, sid)
    assert src.paused_since == NOW and src.sync_status == "paused_reconnect_needed"
    assert await _notice_events(admin) == ["knowledge_pause_warning"]
    async with tenant_session(admin) as s:
        conn = (
            await s.execute(
                select(InstanceConnection).where(InstanceConnection.connection_id == cid)
            )
        ).scalar_one()
        assert conn.status == "error"  # the chip is honest too (round 5B item 12)
