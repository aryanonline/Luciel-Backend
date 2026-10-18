"""The account purge sweeps the tenant's export bundles (round 7 WP-5, item 4) — and a
Luciel hard delete does not (round 7 money review, finding 1).

Live condition (item 4): ``cascade.hard_delete_cascade`` swept secrets, archived
transcripts and knowledge originals by tenant prefix, but never
``vm-export-bundles/{admin_id}/`` — an export bundle is every table the tenant owns in
one object, so a full copy of the data outlived "permanently deleted" for as long as
the bucket's lifecycle backstop let it. Same best-effort-with-audit posture as the
sibling sweeps: a broken store never strands a deletion the owner confirmed, and the
outcome is on the audit trail either way.

Live condition (finding 1): the sweep ran in the Luciel-scope cascade too, so the
day-30 grace expiry of a Luciel whose account stays open (or stays reopenable after a
closure) destroyed a 7-day export link the owner had just been handed. Those bundles
now ride out their link; the bucket's lifecycle rule expires them a day after it.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.lifecycle import cascade
from app.lifecycle.export_store import (
    LocalExportStore,
    export_key_for,
    export_prefix_for,
    set_export_store,
)
from app.models.audit import AdminAuditLog
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]


async def _seed(admin_id: uuid.UUID) -> None:
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
        s.add(
            Instance(
                id=uuid.uuid4(),
                admin_id=admin_id,
                name="L",
                state=LucielState.luciel_grace_window,
                embed_key_public_id="vm_live_seed",
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )


@pytest.fixture
def export_store():
    LocalExportStore.reset()
    store = LocalExportStore()
    set_export_store(store)
    yield store
    set_export_store(None)
    LocalExportStore.reset()


async def _sweep_row(admin_id: uuid.UUID) -> AdminAuditLog:
    async with tenant_session(admin_id) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin_id,
                        AdminAuditLog.event_type == "data_retention_hard_delete",
                    )
                )
            )
            .scalars()
            .all()
        )
    (row,) = [r for r in rows if r.event_payload.get("data_class") == "export_bundle_object"]
    return row


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_account_purge_sweeps_the_tenants_export_bundles_and_no_others(
    export_store,
):
    doomed, neighbour = uuid.uuid4(), uuid.uuid4()
    await _seed(doomed)
    for _ in range(2):
        await export_store.put(key=export_key_for(doomed, uuid.uuid4()), data=b"zip")
    theirs = export_key_for(neighbour, uuid.uuid4())
    await export_store.put(key=theirs, data=b"zip")

    async with tenant_session(doomed) as s:
        await cascade.hard_delete_cascade(s, doomed, include_account_scope=True)

    assert LocalExportStore.keys() == [theirs], "the sweep crossed a tenant boundary"
    row = await _sweep_row(doomed)
    assert row.event_payload["count"] == 2
    assert row.event_payload["error"] is None
    assert row.event_payload["object_prefix"] == export_prefix_for(doomed)
    assert row.event_payload["retention_policy_applied"] == "luciel_hard_delete_cascade"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_broken_export_store_never_strands_the_cascade():
    class Broken:
        async def delete_prefix(self, *, prefix: str) -> int:
            raise RuntimeError("bucket unreachable")

    set_export_store(Broken())  # type: ignore[arg-type]
    try:
        admin = uuid.uuid4()
        await _seed(admin)
        async with tenant_session(admin) as s:
            await cascade.hard_delete_cascade(s, admin, include_account_scope=True)
    finally:
        set_export_store(None)
    async with tenant_session(admin) as s:
        assert (await s.execute(select(Instance))).scalar_one_or_none() is None, (
            "the deletion the owner confirmed still happened"
        )
    row = await _sweep_row(admin)
    assert row.event_payload["count"] == 0
    assert row.event_payload["error"] == "RuntimeError"


async def _bundle_sweep_rows(admin_id: uuid.UUID) -> list[AdminAuditLog]:
    async with tenant_session(admin_id) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin_id,
                        AdminAuditLog.event_type == "data_retention_hard_delete",
                    )
                )
            )
            .scalars()
            .all()
        )
    return [r for r in rows if r.event_payload.get("data_class") == "export_bundle_object"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_luciel_hard_delete_keeps_the_bundles_of_an_account_that_stays(export_store):
    """Luciel-scope cascade (the grace-expiry sweep's call): the account outlives it and
    still holds the 7-day links it was given, so its bundles stay."""
    admin = uuid.uuid4()
    await _seed(admin)
    kept = export_key_for(admin, uuid.uuid4())
    await export_store.put(key=kept, data=b"zip")

    async with tenant_session(admin) as s:
        await cascade.hard_delete_cascade(s, admin)

    assert LocalExportStore.keys() == [kept]
    assert await _bundle_sweep_rows(admin) == [], "no bundle sweep is claimed"
    async with tenant_session(admin) as s:
        assert (await s.execute(select(Instance))).scalar_one_or_none() is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_grace_expiry_of_a_closed_accounts_luciel_keeps_the_closure_export(
    export_store,
):
    """Luciel deleted on day 0, account closed on day 27 (the closure emails its export
    link), grace window expires on day 30 while the D5 reopen window runs to day 57: the
    closure's own link must outlive the Luciel. The purge at the account's day 30 is
    what finally sweeps it."""
    from datetime import UTC, datetime, timedelta

    from app.lifecycle import close_purge, grace_expiry
    from sqlalchemy import update

    admin = uuid.uuid4()
    await _seed(admin)  # the Luciel is already in its grace window
    day0 = datetime.now(UTC) - timedelta(days=27)
    async with tenant_session(admin) as s:
        await s.execute(
            update(Instance)
            .where(Instance.admin_id == admin)
            .values(grace_window_started_at=day0, soft_deleted_at=day0)
        )
    async with tenant_session(admin) as s:
        await cascade.close_account(s, admin, admin)
    closure_bundle = export_key_for(admin, uuid.uuid4())
    await export_store.put(key=closure_bundle, data=b"zip")

    async with tenant_session(admin) as s:
        counts = await grace_expiry.expire_grace_window_if_due(
            s, admin_id=admin, now=day0 + timedelta(days=30, minutes=1)
        )
    assert counts is not None, "the Luciel's grace window expired"
    assert LocalExportStore.keys() == [closure_bundle], "the closure's export link died"

    async with tenant_session(admin) as s:
        closed_at = (
            await s.execute(select(Account.closed_at).where(Account.id == admin))
        ).scalar_one()
    async with tenant_session(admin) as s:
        assert await close_purge.purge_closed_account_if_due(
            s, admin_id=admin, now=close_purge.reopen_deadline(closed_at) + timedelta(minutes=1)
        )
    assert LocalExportStore.keys() == [], "the purge sweeps what the account left behind"
    (row,) = await _bundle_sweep_rows(admin)
    assert row.event_payload["count"] == 1
