"""Audit hash-chain integrity + tamper-evidence (Architecture §5.2 / §5.3).

Regression coverage for the root cause of the intermittent lifecycle failures: audit
rows were written with ``seq = NULL`` because the ORM column sent an explicit NULL that
overrode the DB ``DEFAULT nextval(...)``. With every seq NULL, the per-admin
``ORDER BY seq`` used by both the writer (chain-head lookup) and ``verify_chain`` was
nondeterministic, so a chain that verified on one read order failed on another.

These tests assert the two properties that prevent regression:
  * seq is populated and strictly monotonic per insert (deterministic ordering).
  * the chain is still tamper-evident: mutating any hashed field (payload or the
    app-assigned created_at) breaks verify_chain.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.audit import log as audit
from app.audit.log import verify_chain
from app.db.session import tenant_session
from app.models.audit import ActorType, AdminAuditLog
from sqlalchemy import select, text, update

pytestmark = [pytest.mark.asyncio]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_seq_is_populated_and_monotonic():
    admin = uuid.uuid4()
    async with tenant_session(admin) as s:
        for i in range(5):
            await audit.emit(
                s,
                admin_id=admin,
                event_type="instance_configured",
                actor_type=ActorType.admin_user,
                payload={"i": i},
            )
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog)
                    .where(AdminAuditLog.admin_id == admin)
                    .order_by(AdminAuditLog.seq.asc())
                )
            )
            .scalars()
            .all()
        )
        seqs = [r.seq for r in rows]
        assert all(sq is not None for sq in seqs), f"seq must never be NULL, got {seqs}"
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)  # strictly unique → strict order


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_many_rows_in_one_transaction_chain_correctly():
    # The hard-delete cascade writes many audit rows in ONE transaction (created_at is
    # txn-constant). Without a real seq this was the nondeterministic case. Assert the
    # chain verifies regardless.
    admin = uuid.uuid4()
    async with tenant_session(admin) as s:
        for i in range(10):
            await audit.emit(
                s,
                admin_id=admin,
                event_type="data_retention_hard_delete",
                actor_type=ActorType.platform,
                payload={"data_class": f"class_{i}"},
            )
    async with tenant_session(admin) as s:
        assert await verify_chain(s, admin) is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_tamper_with_payload_breaks_chain():
    admin = uuid.uuid4()
    async with tenant_session(admin) as s:
        for i in range(3):
            await audit.emit(
                s,
                admin_id=admin,
                event_type="instance_configured",
                actor_type=ActorType.admin_user,
                payload={"surface": f"s{i}"},
            )
    async with tenant_session(admin) as s:
        assert await verify_chain(s, admin) is True
    # Tamper the middle row's payload directly in the DB (bypassing the append-only grant
    # via a raw UPDATE for the test) — the recomputed hash of that row no longer matches
    # the NEXT row's stored prev_hash, so verification must fail.
    async with tenant_session(admin) as s:
        await s.execute(text("GRANT UPDATE ON admin_audit_log TO luciel_app"))
        await s.execute(
            update(AdminAuditLog)
            .where(AdminAuditLog.admin_id == admin)
            .where(AdminAuditLog.event_payload["surface"].astext == "s1")
            .values(event_payload={"surface": "TAMPERED"})
        )
        await s.execute(text("REVOKE UPDATE ON admin_audit_log FROM luciel_app"))
        assert await verify_chain(s, admin) is False


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_tamper_with_created_at_breaks_chain():
    # created_at is part of the hash material (§5.2). Mutating it must break the chain —
    # this is why created_at is app-assigned (exact value persisted = exact value hashed):
    # tamper-evidence over the timestamp is preserved.
    admin = uuid.uuid4()
    async with tenant_session(admin) as s:
        for i in range(3):
            await audit.emit(
                s,
                admin_id=admin,
                event_type="instance_configured",
                actor_type=ActorType.admin_user,
                payload={"surface": f"s{i}"},
            )
    async with tenant_session(admin) as s:
        # Move the FIRST row's timestamp; its recomputed hash changes, breaking the link
        # the second row stored.
        first = (
            await s.execute(
                select(AdminAuditLog)
                .where(AdminAuditLog.admin_id == admin)
                .order_by(AdminAuditLog.seq.asc())
                .limit(1)
            )
        ).scalar_one()
        tampered = (first.created_at or datetime.now(UTC)) + timedelta(seconds=5)
        await s.execute(text("GRANT UPDATE ON admin_audit_log TO luciel_app"))
        await s.execute(
            update(AdminAuditLog)
            .where(AdminAuditLog.event_id == first.event_id)
            .values(created_at=tampered)
        )
        await s.execute(text("REVOKE UPDATE ON admin_audit_log FROM luciel_app"))
        assert await verify_chain(s, admin) is False
