"""Delete / restore against work that is IN FLIGHT at that instant (round 7 WP-5, item 8).

The lifecycle transitions were each tested against a tenant at rest. Three things are
not at rest when an owner clicks Delete (or the day-30 sweep fires):

* an EXPORT being built — the bundle must still be delivered, or the request must fail
  honestly; and a bundle must never land AFTER the hard-delete cascade swept the
  tenant's export prefix (a full copy of the data outliving "hard-deleted");
* a knowledge SYNC whose provider fetch is on the wire — it must not leave the deleted
  Luciel with a second copy of every synced document for restore to bring back beside
  the first, and it is not run again while the Luciel is deleted;
* the CRM LINK rows (where each lead's record lives in the tenant's CRM) — restore
  must re-qualify them against the connection that actually came back, because a
  record id belongs to ONE CRM account.

Every test here drives two real transactions against Postgres; the interleaving is
forced at the provider / object-store seam, which is exactly where the live system
spends its seconds.
"""

from __future__ import annotations

import asyncio
import io
import uuid
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from app.api.deps import Subject
from app.api.errors import ApiError
from app.api.v1 import exports as exports_route
from app.api.v1.admin import account as account_route
from app.audit import log as audit
from app.db.session import tenant_session
from app.knowledge import sweep as knowledge_sweep
from app.knowledge import sync
from app.knowledge.sync import RemoteItem, RemoteSnapshot
from app.lifecycle import close_purge, grace_expiry, state_machine
from app.lifecycle.export_link import parse_export_token
from app.lifecycle.export_store import (
    LocalExportStore,
    export_key_for,
    export_prefix_for,
    set_export_store,
)
from app.models.audit import ActorType, AdminAuditLog
from app.models.connection import QUIESCING, InstanceConnection
from app.models.crm_link import CrmLeadLink
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.knowledge import KnowledgeSource
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

NOW = datetime.now(UTC)


async def _seed(
    *,
    account_state: AccountState = AccountState.verified,
    luciel_state: LucielState = LucielState.active,
    grace_started: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin}@x.com",
                email_lower=f"{admin}@x.com",
                password_hash="x",
                state=account_state,
                email_verified=True,
            )
        )
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="L",
                state=luciel_state,
                embed_key_public_id=f"vm_live_{uuid.uuid4().hex[:10]}",
                grace_window_started_at=grace_started,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
    return admin, iid


def _conn(admin, iid, ctype, provider, status, *, stashed=None) -> InstanceConnection:
    return InstanceConnection(
        connection_id=uuid.uuid4(),
        admin_id=admin,
        instance_id=iid,
        connection_type=ctype,
        auth_class="oauth_token",
        provider=provider,
        status=status,
        status_stashed=stashed,
        non_secret_config={},
    )


@pytest.fixture
def export_store():
    LocalExportStore.reset()
    yield
    set_export_store(None)
    LocalExportStore.reset()


async def _events(admin: uuid.UUID, event_type: str) -> list[dict[str, object]]:
    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog)
                    .where(AdminAuditLog.admin_id == admin, AdminAuditLog.event_type == event_type)
                    .order_by(AdminAuditLog.seq.asc())
                )
            )
            .scalars()
            .all()
        )
    return [dict(r.event_payload) for r in rows]


# --------------------------------------------------------------------------- #
# Exports                                                                      #
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset", "export_store")
async def test_a_luciel_delete_during_an_inflight_export_still_delivers_the_bundle():
    """The owner clicks Export, then Delete in another tab while the ZIP is uploading.
    The delete commits first; the export must still land, link and all."""
    admin, iid = await _seed()
    async with tenant_session(admin) as s:
        s.add(_conn(admin, iid, "calendar", "google_calendar", "connected"))

    class DeleteLandsDuringUpload(LocalExportStore):
        async def put(self, *, key: str, data: bytes) -> None:
            async with tenant_session(admin) as other:
                await state_machine.delete_luciel(other, admin, admin)
            await super().put(key=key, data=data)

    store = DeleteLandsDuringUpload()
    set_export_store(store)
    result = await account_route.request_export(
        subject=Subject(admin_id=admin, user_id=admin, session_epoch=0), store=store
    )

    token = result.download_url.rsplit("/", 1)[1]
    tenant, export_id = parse_export_token(token)
    assert tenant == admin
    key = export_key_for(admin, export_id)
    data = LocalExportStore.get_bytes(key)
    assert data is not None, "the bundle the owner was promised never reached the store"
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert "instance.json" in z.namelist()
    assert result.email_outcome == "recorded"
    # The door opens: the 7-day link resolves to the object that was just written.
    opened = await exports_route.download_export(token)
    assert opened.status_code == 302
    assert key in opened.headers["location"]

    async with tenant_session(admin) as s:
        inst = (await s.execute(select(Instance))).scalar_one()
        conn = (await s.execute(select(InstanceConnection))).scalar_one()
    assert inst.state == LucielState.luciel_grace_window
    assert conn.status == QUIESCING
    assert len(await _events(admin, "data_export_ready")) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset", "export_store")
async def test_the_hard_delete_cascade_waits_for_an_inflight_export_and_sweeps_its_bundle():
    """Day 30 of the grace window fires while an export upload is on the wire. The
    cascade's prefix sweep used to run first and find nothing; the upload then landed
    a full copy of the tenant under a prefix nothing would ever sweep again."""
    admin, _ = await _seed(
        luciel_state=LucielState.luciel_grace_window, grace_started=NOW - timedelta(days=31)
    )
    uploading, release = asyncio.Event(), asyncio.Event()

    class SlowUpload(LocalExportStore):
        async def put(self, *, key: str, data: bytes) -> None:
            uploading.set()
            await release.wait()
            await super().put(key=key, data=data)

    store = SlowUpload()
    set_export_store(store)
    export_task = asyncio.create_task(
        account_route.request_export(
            subject=Subject(admin_id=admin, user_id=admin, session_epoch=0), store=store
        )
    )
    await asyncio.wait_for(uploading.wait(), timeout=20)

    async def _expire() -> dict[str, int] | None:
        async with tenant_session(admin) as s:
            return await grace_expiry.expire_grace_window_if_due(s, admin_id=admin, now=NOW)

    purge_task = asyncio.create_task(_expire())
    done, _ = await asyncio.wait({purge_task}, timeout=1.5)
    try:
        assert not done, "the cascade swept the export prefix while a bundle was still uploading"
    finally:
        release.set()
        await asyncio.gather(export_task, purge_task, return_exceptions=True)

    assert export_task.exception() is None
    assert purge_task.result() is not None, "the cascade ran once the export finished"
    assert LocalExportStore.keys() == [], "a bundle outlived the hard delete"
    (sweep,) = [
        p
        for p in await _events(admin, "data_retention_hard_delete")
        if p.get("data_class") == "export_bundle_object"
    ]
    assert sweep["count"] == 1
    assert sweep["object_prefix"] == export_prefix_for(admin)
    # The link the owner was handed a moment before the Luciel was destroyed says so
    # honestly instead of redirecting to an object that is no longer there.
    token = export_task.result().download_url.rsplit("/", 1)[1]
    with pytest.raises(ApiError) as gone:
        await exports_route.download_export(token)
    assert gone.value.status_code == 410


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset", "export_store")
async def test_an_export_that_loses_the_race_to_the_account_purge_fails_honestly():
    """A closed account's owner re-requests the export at the instant the day-30 purge
    runs. The purge wins; the export must not write a bundle for a tenant that no
    longer exists, and must not answer "ready"."""
    admin, _ = await _seed(
        account_state=AccountState.closed,
        luciel_state=LucielState.luciel_grace_window,
        grace_started=NOW - timedelta(days=31),
    )
    async with tenant_session(admin) as s:
        await audit.emit(
            s,
            admin_id=admin,
            event_type="account_closure_initiated",
            actor_type=ActorType.admin_user,
            actor_user_id=admin,
        )
    sweeping, release = asyncio.Event(), asyncio.Event()

    class SlowSweep(LocalExportStore):
        async def delete_prefix(self, *, prefix: str) -> int:
            sweeping.set()
            await release.wait()
            return await super().delete_prefix(prefix=prefix)

    store = SlowSweep()
    set_export_store(store)

    async def _purge() -> bool:
        async with tenant_session(admin) as s:
            return await close_purge.purge_closed_account_if_due(
                s, admin_id=admin, now=datetime.now(UTC) + timedelta(days=31)
            )

    purge_task = asyncio.create_task(_purge())
    await asyncio.wait_for(sweeping.wait(), timeout=20)
    export_task = asyncio.create_task(
        account_route.request_export(
            subject=Subject(admin_id=admin, user_id=admin, session_epoch=0), store=store
        )
    )
    done, _ = await asyncio.wait({export_task}, timeout=1.5)
    try:
        assert not done, "the export built a bundle beside a purge that was already running"
    finally:
        release.set()
        await asyncio.gather(export_task, purge_task, return_exceptions=True)

    assert purge_task.result() is True
    failure = export_task.exception()
    assert isinstance(failure, ApiError), failure
    assert failure.status_code == 401
    assert LocalExportStore.keys() == [], "a bundle was written for a purged tenant"
    assert await _events(admin, "data_export_ready") == []
    assert await _events(admin, "data_export_self_serve") == []


# --------------------------------------------------------------------------- #
# Knowledge sync                                                               #
# --------------------------------------------------------------------------- #
ITEMS = [
    RemoteItem(external_id="doc1", name="Playbook A", text="alpha pricing 899", revision="r1"),
    RemoteItem(external_id="doc2", name="Playbook B", text="beta hours weekdays", revision="r1"),
]


class _Drive:
    origin = "google_drive"

    def __init__(self) -> None:
        self.calls = 0

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        self.calls += 1
        return RemoteSnapshot(items=list(ITEMS))


async def _live_sources(admin: uuid.UUID) -> list[KnowledgeSource]:
    async with tenant_session(admin) as s:
        return list(
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


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_sync_in_flight_at_the_delete_is_not_resumed_and_restore_brings_back_one_copy(
    monkeypatch,
):
    """The provider fetch is on the wire when the owner deletes the Luciel. The sync
    finishes against a tenant whose sources were all tombstoned a moment ago, so it
    ingests every document again. Restore used to revive the tombstoned originals
    beside those copies — two sources per document, one of them orphaned forever."""
    admin, iid = await _seed()
    conn = _conn(admin, iid, "knowledge_source", "google_drive", "connected")
    cid = conn.connection_id
    async with tenant_session(admin) as s:
        s.add(conn)
    provider = _Drive()
    async with tenant_session(admin) as s:
        await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=cid,
            origin="google_drive",
            provider=provider,
        )
    originals = {src.external_id: src.id for src in await _live_sources(admin)}
    assert set(originals) == {"doc1", "doc2"}

    class DeleteLandsDuringFetch(_Drive):
        async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
            async with tenant_session(admin) as other:
                await state_machine.delete_luciel(other, admin, admin)
            return await super().snapshot(connection_id)

    async with tenant_session(admin) as s:
        await sync.sync_connection(
            s,
            admin_id=admin,
            instance_id=iid,
            connection_id=cid,
            origin="google_drive",
            provider=DeleteLandsDuringFetch(),
        )

    # The in-flight run did not un-delete anything: the connection is still draining
    # with its pre-delete status stashed, and the Luciel is still in its grace window.
    async with tenant_session(admin) as s:
        row = (await s.execute(select(InstanceConnection))).scalar_one()
        inst = (await s.execute(select(Instance))).scalar_one()
    assert (row.status, row.status_stashed) == (QUIESCING, "connected")
    assert inst.state == LucielState.luciel_grace_window

    # NOT RESUMED: the scheduled sweep has nothing to do for a deleted Luciel, however
    # overdue its sources are — the provider is not called again during the grace window.
    later = _Drive()
    monkeypatch.setattr(knowledge_sweep, "provider_for_connection", lambda c, store: later)
    async with tenant_session(admin) as s:
        synced = await knowledge_sweep.sync_due_connections(
            s, admin_id=admin, now=datetime.now(UTC) + timedelta(days=3)
        )
    assert (synced, later.calls) == (0, 0)

    async with tenant_session(admin) as s:
        await state_machine.restore(s, admin, admin)

    live = await _live_sources(admin)
    assert sorted(src.external_id or "" for src in live) == ["doc1", "doc2"], (
        "restore brought back a second copy of a synced document"
    )
    # "Exactly as it was": the sources that come back are the ones the delete put away.
    assert {src.external_id: src.id for src in live} == originals
    async with tenant_session(admin) as s:
        total = len((await s.execute(select(KnowledgeSource))).scalars().all())
    assert total == 2, "the grace-window copies were left behind as tombstones"
    (dedupe,) = [
        p
        for p in await _events(admin, "knowledge_source_edited")
        if p.get("action") == "restore_duplicates_removed"
    ]
    assert dedupe["count"] == 2

    # ...and the sweep picks the restored connection up again on its normal cadence.
    async with tenant_session(admin) as s:
        synced = await knowledge_sweep.sync_due_connections(
            s, admin_id=admin, now=datetime.now(UTC) + timedelta(days=3)
        )
    assert (synced, later.calls) == (1, 1)
    assert len(await _live_sources(admin)) == 2


# --------------------------------------------------------------------------- #
# CRM link rows                                                                #
# --------------------------------------------------------------------------- #
async def _seed_links(admin: uuid.UUID, iid: uuid.UUID, provider: str, n: int = 2) -> None:
    async with tenant_session(admin) as s:
        for i in range(n):
            s.add(
                CrmLeadLink(
                    admin_id=admin,
                    instance_id=iid,
                    provider=provider,
                    lead_id=uuid.uuid4(),
                    lead_key=f"lead{i}@example.com",
                    crm_record_id=f"{provider}-rec-{i}",
                )
            )


async def _links(admin: uuid.UUID) -> list[CrmLeadLink]:
    async with tenant_session(admin) as s:
        return list((await s.execute(select(CrmLeadLink))).scalars().all())


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_restore_keeps_crm_links_when_the_same_crm_connection_comes_back():
    """The credential survived the grace window untouched, so the record ids still name
    records in the same CRM account: the next push updates in place, no duplicate."""
    admin, iid = await _seed()
    async with tenant_session(admin) as s:
        s.add(_conn(admin, iid, "crm", "hubspot", "connected"))
    await _seed_links(admin, iid, "hubspot")

    async with tenant_session(admin) as s:
        await state_machine.delete_luciel(s, admin, admin)
    assert len(await _links(admin)) == 2, "the grace window keeps the links (restore needs them)"
    async with tenant_session(admin) as s:
        await state_machine.restore(s, admin, admin)

    assert {link.crm_record_id for link in await _links(admin)} == {
        "hubspot-rec-0",
        "hubspot-rec-1",
    }
    assert await _events(admin, "crm_links_cleared") == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_restore_clears_crm_links_when_the_crm_was_reconnected_during_grace():
    """The owner re-attached the CRM while the Luciel was deleted — a fresh consent,
    possibly against a different portal. A record id from the old account would PATCH
    whatever contact carries that id in the new one."""
    admin, iid = await _seed()
    conn = _conn(admin, iid, "crm", "hubspot", "connected")
    cid = conn.connection_id
    async with tenant_session(admin) as s:
        s.add(conn)
    await _seed_links(admin, iid, "hubspot")

    async with tenant_session(admin) as s:
        await state_machine.delete_luciel(s, admin, admin)
    async with tenant_session(admin) as s:
        # What a connect during grace does: the draining row is the type's single
        # active slot, so the new credential lands on it and it leaves the drain.
        row = (await s.execute(select(InstanceConnection))).scalar_one()
        row.status = "connected"
    async with tenant_session(admin) as s:
        await state_machine.restore(s, admin, admin)

    assert await _links(admin) == []
    (cleared,) = await _events(admin, "crm_links_cleared")
    assert cleared["count"] == 2
    assert cleared["reason"] == "restore_crm_connection_changed"
    assert cleared["connection_id"] == str(cid)
    assert "lead0@example.com" not in str(cleared), "the audit row carries a count, never a key"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_restore_clears_crm_links_when_no_crm_connection_comes_back_intact():
    """A pre-0049 delete left no stash: the row restores as ``unconfigured`` and the
    owner has to reconnect — against whichever account they pick. A superseded row
    (a NEW live CRM row attached during a pre-WP-5 grace window, here on another
    provider) is the same story for the other provider's links."""
    legacy, legacy_iid = await _seed(luciel_state=LucielState.luciel_grace_window)
    async with tenant_session(legacy) as s:
        s.add(_conn(legacy, legacy_iid, "crm", "hubspot", "revoked"))
    await _seed_links(legacy, legacy_iid, "hubspot")
    async with tenant_session(legacy) as s:
        await state_machine.restore(s, legacy, legacy)
    assert await _links(legacy) == []
    (cleared,) = await _events(legacy, "crm_links_cleared")
    assert cleared["count"] == 2

    swapped, swapped_iid = await _seed(luciel_state=LucielState.luciel_grace_window)
    async with tenant_session(swapped) as s:
        s.add(_conn(swapped, swapped_iid, "crm", "hubspot", "revoked", stashed="connected"))
        s.add(_conn(swapped, swapped_iid, "crm", "salesforce", "connected"))
    await _seed_links(swapped, swapped_iid, "hubspot")
    await _seed_links(swapped, swapped_iid, "salesforce", n=1)
    async with tenant_session(swapped) as s:
        await state_machine.restore(s, swapped, swapped)
    assert await _links(swapped) == [], "neither provider's links name a credential that survived"
    assert sum(int(str(p["count"])) for p in await _events(swapped, "crm_links_cleared")) == 3
