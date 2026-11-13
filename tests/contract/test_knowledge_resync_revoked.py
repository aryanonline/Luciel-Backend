"""Round 7 WP-4c #7: a manual re-sync on a revoked grant used to leave a green chip.

Both manual routes (POST /sync-connections/{id}/sync and POST /sources/{id}/resync)
paused the sources and raised the 409 inside the request's ``tenant_session`` —
whose transaction the 409 rolled back, so even the pause was lost — and never
touched the connection row, so the Configure page kept "Connected" on a credential
the provider had just refused. The sweep did both halves (round 5B item 12: pause +
``health.mark_connection_broken`` → audit + once-per-incident attention email); the
routes now do the same, in their own transaction, before the 409.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.knowledge import KnowledgeSource
from sqlalchemy import select

from tests.contract.test_dashboard import _signup_verify_login

pytestmark = [pytest.mark.contract]


async def _ready(client) -> uuid.UUID:
    admin = await _signup_verify_login(client)
    await client.post(
        "/api/v1/admin/luciel", json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"}
    )
    return admin


async def _drive_connection(
    client, admin: uuid.UUID, *, status: str
) -> tuple[uuid.UUID, uuid.UUID]:
    """A Drive connection in ``status`` with one synced source and NO token on file —
    the shape a revoked grant leaves behind (the provider raises ProviderUnavailable)."""
    rc = await client.post(
        "/api/v1/admin/knowledge/sync-connections", json={"provider": "google_drive"}
    )
    assert rc.status_code == 201, rc.text
    cid = uuid.UUID(rc.json()["connectionId"])
    sid = uuid.uuid4()
    async with tenant_session(admin) as s:
        conn = (
            await s.execute(
                select(InstanceConnection).where(InstanceConnection.connection_id == cid)
            )
        ).scalar_one()
        conn.status = status
        s.add(
            KnowledgeSource(
                id=sid,
                admin_id=admin,
                instance_id=conn.instance_id,
                name="Playbook.pdf",
                origin="google_drive",
                source_origin="google_drive",
                external_id="drive-1",
                connection_id=cid,
                ingestion_status="ready",
                sync_status="synced",
            )
        )
    return cid, sid


async def _state(
    admin: uuid.UUID, cid: uuid.UUID, sid: uuid.UUID
) -> tuple[InstanceConnection, KnowledgeSource, list[str], int]:
    async with tenant_session(admin) as s:
        conn = (
            await s.execute(
                select(InstanceConnection).where(InstanceConnection.connection_id == cid)
            )
        ).scalar_one()
        src = (
            await s.execute(select(KnowledgeSource).where(KnowledgeSource.id == sid))
        ).scalar_one()
        events = [
            r.event_type
            for r in (
                await s.execute(
                    select(AdminAuditLog)
                    .where(AdminAuditLog.admin_id == admin)
                    .order_by(AdminAuditLog.created_at)
                )
            ).scalars()
        ]
        return conn, src, events, events.count("connection_attention_emailed")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_trigger_sync_on_a_dead_grant_flips_the_chip_and_pauses_the_sources(client):
    admin = await _ready(client)
    cid, sid = await _drive_connection(client, admin, status="connected")

    rs = await client.post(f"/api/v1/admin/knowledge/sync-connections/{cid}/sync")
    assert rs.status_code == 409 and "unavailable" in rs.json()["message"].lower()

    conn, src, events, emails = await _state(admin, cid, sid)
    # The chip is honest: the row reads error with the reconnect detail, audited.
    assert conn.status == "error" and "reconnect" in (conn.status_detail or "")
    assert "connection_status_changed" in events
    # The sources are paused (this used to be rolled back with the 409) and the D10
    # clock is running.
    assert src.sync_status == "paused_reconnect_needed" and src.paused_since is not None
    # The owner was told, once — the health transition machinery, not a bespoke mail.
    assert emails == 1

    # A second click is not a second incident: still error, still one email.
    rs2 = await client.post(f"/api/v1/admin/knowledge/sync-connections/{cid}/sync")
    assert rs2.status_code == 409
    conn, _src, _events, emails = await _state(admin, cid, sid)
    assert conn.status == "error" and emails == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_resync_source_on_a_dead_grant_does_the_same(client):
    admin = await _ready(client)
    cid, sid = await _drive_connection(client, admin, status="connected")

    rs = await client.post(f"/api/v1/admin/knowledge/sources/{sid}/resync")
    assert rs.status_code == 409 and "unavailable" in rs.json()["message"].lower()

    conn, src, events, emails = await _state(admin, cid, sid)
    assert conn.status == "error" and "connection_status_changed" in events
    assert src.sync_status == "paused_reconnect_needed" and src.paused_since is not None
    assert emails == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_connection_the_owner_never_connected_is_not_flipped_to_error(client):
    """Flipping a never-connected row to "error" would claim a grant it never had; it stays
    unconfigured (the honest setup state) and no attention email goes out."""
    admin = await _ready(client)
    cid, sid = await _drive_connection(client, admin, status="unconfigured")

    rs = await client.post(f"/api/v1/admin/knowledge/sync-connections/{cid}/sync")
    assert rs.status_code == 409

    conn, src, events, emails = await _state(admin, cid, sid)
    assert conn.status == "unconfigured"
    assert "connection_status_changed" not in events and emails == 0
    assert src.sync_status == "paused_reconnect_needed"  # never silently served


# --- Round 7 Phase 2 review, knowledge #2: an unreachable site is not a dead grant ------
# A crawl whose every page failed raised the same ProviderUnavailable as a revoked
# grant, so both manual routes ran _flag_dead_credential on a connection that has no
# credential: the D10 stop clock started, the chip went red with "reconnect", and the
# owner got the attention email. The 409 wording is unchanged; the bookkeeping is now
# an ordinary failed sync.


async def _crawl_connection(client, admin: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    rc = await client.post(
        "/api/v1/admin/knowledge/sync-connections",
        json={"provider": "website_crawl", "crawlUrls": ["https://acme.example/"]},
    )
    assert rc.status_code == 201, rc.text
    cid = uuid.UUID(rc.json()["connectionId"])
    sid = uuid.uuid4()
    async with tenant_session(admin) as s:
        conn = (
            await s.execute(
                select(InstanceConnection).where(InstanceConnection.connection_id == cid)
            )
        ).scalar_one()
        assert conn.status == "connected"
        s.add(
            KnowledgeSource(
                id=sid,
                admin_id=admin,
                instance_id=conn.instance_id,
                name="https://acme.example/",
                origin="website_crawl",
                source_origin="website_crawl",
                external_id="https://acme.example/",
                connection_id=cid,
                ingestion_status="ready",
                sync_status="synced",
            )
        )
    return cid, sid


class _UnreachableSite:
    origin = "website_crawl"

    async def snapshot(self, connection_id: uuid.UUID):  # noqa: ANN201
        from app.knowledge.providers import CrawlUnreachable

        raise CrawlUnreachable("crawl fetched no pages: https://acme.example/")


@pytest.mark.parametrize("route", ["trigger_sync", "resync_source"])
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_crawl_that_reached_no_page_is_a_failed_sync_not_a_dead_grant(
    client, monkeypatch, route: str
):
    import app.api.v1.admin.knowledge as knowledge_routes

    admin = await _ready(client)
    cid, sid = await _crawl_connection(client, admin)
    monkeypatch.setattr(
        knowledge_routes, "provider_for_connection", lambda conn, store: _UnreachableSite()
    )

    url = (
        f"/api/v1/admin/knowledge/sync-connections/{cid}/sync"
        if route == "trigger_sync"
        else f"/api/v1/admin/knowledge/sources/{sid}/resync"
    )
    rs = await client.post(url)
    # The owner-facing answer is unchanged.
    assert rs.status_code == 409
    assert rs.json()["message"] == (
        "Sync unavailable: crawl fetched no pages: https://acme.example/"
    )

    conn, src, events, emails = await _state(admin, cid, sid)
    # A failed sync, durable past the 409's rollback ...
    assert src.sync_status == "error"
    # ... and NOT a revocation: no stop clock, chip still connected, nobody emailed.
    assert src.paused_since is None
    assert conn.status == "connected" and conn.status_detail is None
    assert "connection_status_changed" not in events and emails == 0
