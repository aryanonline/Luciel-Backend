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
from sqlalchemy import select, text

from tests.conftest import superuser_exec


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
    # Tamper the middle row's payload directly in the DB — the recomputed hash of that
    # row no longer matches the NEXT row's stored prev_hash, so verification must fail.
    # After the B1 immutability lock, the app role CANNOT mutate admin_audit_log
    # (proved separately in test_app_role_cannot_mutate_audit_log); a deliberate tamper
    # for this tamper-evidence test must therefore take the superuser path.
    # The guard trigger blocks UPDATE for every role (including superuser), which is
    # the point; to inject a deliberate tamper we disable it as superuser, mutate,
    # then re-enable. This proves the chain verifier still detects the tamper.
    superuser_exec("ALTER TABLE admin_audit_log DISABLE TRIGGER trg_audit_append_only")
    superuser_exec(
        'UPDATE admin_audit_log SET event_payload = \'{"surface":"TAMPERED"}\'::jsonb '
        f"WHERE admin_id = '{admin}' AND event_payload->>'surface' = 's1'"
    )
    superuser_exec("ALTER TABLE admin_audit_log ENABLE TRIGGER trg_audit_append_only")
    async with tenant_session(admin) as s:
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
    # Move the FIRST row's timestamp; its recomputed hash changes, breaking the link
    # the second row stored. Read the target in one session, tamper externally, then
    # verify in a FRESH session so the committed external change is visible.
    async with tenant_session(admin) as s:
        first = (
            await s.execute(
                select(AdminAuditLog)
                .where(AdminAuditLog.admin_id == admin)
                .order_by(AdminAuditLog.seq.asc())
                .limit(1)
            )
        ).scalar_one()
        tampered = (first.created_at or datetime.now(UTC)) + timedelta(seconds=5)
    # Superuser path with the guard temporarily disabled (the app role cannot mutate
    # the audit log post-B1-lock, and the guard blocks even superuser).
    superuser_exec("ALTER TABLE admin_audit_log DISABLE TRIGGER trg_audit_append_only")
    superuser_exec(
        "UPDATE admin_audit_log SET created_at = '"
        + tampered.isoformat()
        + f"' WHERE event_id = '{first.event_id}'"
    )
    superuser_exec("ALTER TABLE admin_audit_log ENABLE TRIGGER trg_audit_append_only")
    async with tenant_session(admin) as s:
        assert await verify_chain(s, admin) is False


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_app_role_cannot_mutate_audit_log():
    """B1 regression (Architecture §5.3): the append-only guarantee must be a true
    lock, not a revocable grant. The application role can INSERT + SELECT but must
    NOT be able to UPDATE/DELETE, re-grant itself those privileges, or drop the
    guard — because admin_audit_log is owned by luciel_audit_owner, a role the app
    role is not a member of."""
    admin = uuid.uuid4()
    async with tenant_session(admin) as s:
        await audit.emit(
            s,
            admin_id=admin,
            event_type="instance_created",
            actor_type=ActorType.platform,
            payload={"k": "v"},
        )

    # UPDATE must be rejected.
    async with tenant_session(admin) as s:
        with pytest.raises(Exception):  # noqa: B017 - asyncpg perm error class varies
            await s.execute(text("UPDATE admin_audit_log SET event_type = 'x'"))
    # DELETE must be rejected.
    async with tenant_session(admin) as s:
        with pytest.raises(Exception):  # noqa: B017
            await s.execute(text("DELETE FROM admin_audit_log"))
    # A self-grant must not actually confer UPDATE: even if the GRANT statement is
    # accepted as a no-op, a subsequent UPDATE must still fail.
    async with tenant_session(admin) as s:
        try:
            await s.execute(text("GRANT UPDATE ON admin_audit_log TO luciel_app"))
        except Exception:
            pass  # rejection is fine; no-op acceptance is also fine
    async with tenant_session(admin) as s:
        with pytest.raises(Exception):  # noqa: B017
            await s.execute(text("UPDATE admin_audit_log SET event_type = 'x'"))
    # Dropping the guard trigger must be rejected (not the owner).
    async with tenant_session(admin) as s:
        with pytest.raises(Exception):  # noqa: B017
            await s.execute(text("DROP TRIGGER trg_audit_append_only ON admin_audit_log"))


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_chain_is_independent_per_admin_with_genesis_null():
    """Axis 9 F-04 (Architecture §5.3): the hash chain is PER admin_id — the first
    row for each tenant is genesis (prev_hash = NULL), and one tenant's rows never
    chain from another's. Verify both tenants' chains independently."""
    admin_a = uuid.uuid4()
    admin_b = uuid.uuid4()
    for admin in (admin_a, admin_b):
        async with tenant_session(admin) as s:
            for i in range(3):
                await audit.emit(
                    s,
                    admin_id=admin,
                    event_type="instance_configured",
                    actor_type=ActorType.admin_user,
                    payload={"i": i},
                )
    # Each tenant's first row is genesis (prev_hash NULL); each chain verifies on its own.
    for admin in (admin_a, admin_b):
        async with tenant_session(admin) as s:
            first = (
                await s.execute(
                    select(AdminAuditLog)
                    .where(AdminAuditLog.admin_id == admin)
                    .order_by(AdminAuditLog.seq.asc())
                    .limit(1)
                )
            ).scalar_one()
            assert first.prev_hash is None, "first row per admin_id must be genesis (NULL)"
            assert await verify_chain(s, admin) is True
