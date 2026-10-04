"""The hard-delete cascade sweeps the tenant's export bundles (round 7 WP-5, item 4).

Live condition: ``cascade.hard_delete_cascade`` swept secrets, archived transcripts and
knowledge originals by tenant prefix, but never ``vm-export-bundles/{admin_id}/`` — an
export bundle is every table the tenant owns in one object, so a full copy of the data
outlived "hard-deleted" for as long as the bucket's lifecycle backstop let it. Same
best-effort-with-audit posture as the sibling sweeps: a broken store never strands a
deletion the owner confirmed, and the outcome is on the audit trail either way.
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
async def test_the_cascade_sweeps_the_tenants_export_bundles_and_no_others(export_store):
    doomed, neighbour = uuid.uuid4(), uuid.uuid4()
    await _seed(doomed)
    for _ in range(2):
        await export_store.put(key=export_key_for(doomed, uuid.uuid4()), data=b"zip")
    theirs = export_key_for(neighbour, uuid.uuid4())
    await export_store.put(key=theirs, data=b"zip")

    async with tenant_session(doomed) as s:
        await cascade.hard_delete_cascade(s, doomed)

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
            await cascade.hard_delete_cascade(s, admin)
    finally:
        set_export_store(None)
    async with tenant_session(admin) as s:
        assert (await s.execute(select(Instance))).scalar_one_or_none() is None, (
            "the deletion the owner confirmed still happened"
        )
    row = await _sweep_row(admin)
    assert row.event_payload["count"] == 0
    assert row.event_payload["error"] == "RuntimeError"
