"""Both audit artifacts are append-only in fact, not just by convention (B4 / §5.11).

Audit finding B4: ``internal_access_log`` (§5.11 operator wall / Commitment C23) was
described as "the same pattern as admin_audit_log" but was not. Migration 0012 created
it under the app-role DSN, so ``luciel_app`` OWNED it, and it revoked only
``UPDATE, DELETE``. TRUNCATE is a separate privilege that is inherent to the owner, and
the row-level guard 0012 installed cannot fire on TRUNCATE at all — so the app role
could erase the entire staff-access trail in one statement.

The property under test is the commitment itself, stated in terms of the role the
application actually connects as: for BOTH tables, INSERT and SELECT work, every
mutation (UPDATE / DELETE / TRUNCATE) is denied, and the app role does not own the
table — because ownership, not the REVOKE, is what makes the denial unbypassable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.audit import log as audit
from app.audit.internal_access import ACCESS_TYPE_BREAK_GLASS, emit_internal_access
from app.audit.log import verify_chain
from app.db.session import tenant_session, untenanted_session
from app.models.audit import ActorType
from app.models.internal_access import InternalAccessLog
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

pytestmark = [pytest.mark.asyncio]

AUDIT_TABLES = ["admin_audit_log", "internal_access_log"]


async def _expect_denied(sql: str) -> None:
    """Run one statement as the app role and require Postgres to refuse it.

    Each attempt gets its own session: a refusal aborts the transaction, so sharing
    one would make later attempts fail for the wrong reason. Both refusal paths are
    accepted — a missing grant (privilege check) and the guard trigger (which raises
    with ERRCODE insufficient_privilege) — because the commitment is that the mutation
    does not happen, not which layer stops it.
    """
    with pytest.raises(DBAPIError):
        async with untenanted_session() as s:
            await s.execute(text(sql))


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize("table", AUDIT_TABLES)
async def test_app_role_cannot_truncate_an_audit_table(table: str):
    """The B4 hole: TRUNCATE, the one mutation neither the REVOKE nor the row-level
    guard covered."""
    await _expect_denied(f"TRUNCATE TABLE {table}")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize("table", AUDIT_TABLES)
async def test_app_role_cannot_delete_from_an_audit_table(table: str):
    await _expect_denied(f"DELETE FROM {table}")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize("table", AUDIT_TABLES)
async def test_app_role_cannot_update_an_audit_table(table: str):
    await _expect_denied(f"UPDATE {table} SET created_at = now()")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize("table", AUDIT_TABLES)
async def test_app_role_does_not_own_the_audit_table(table: str):
    """Ownership is the actual lock. An owner can re-GRANT itself UPDATE/DELETE,
    TRUNCATE regardless of grants, and DROP the guard — so if this regresses, every
    assertion above becomes bypassable in one statement."""
    async with untenanted_session() as s:
        owner = (
            await s.execute(
                text(
                    "SELECT r.rolname FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "JOIN pg_roles r ON r.oid = c.relowner "
                    "WHERE n.nspname = 'public' AND c.relname = :t"
                ),
                {"t": table},
            )
        ).scalar_one()
        current = (await s.execute(text("SELECT current_user"))).scalar_one()
    assert owner != current, f"{table} is owned by the app role {current} — lock bypassable"
    assert owner == "luciel_audit_owner"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize("table", AUDIT_TABLES)
async def test_app_role_is_not_a_member_of_the_owner_role(table: str):
    """Membership would hand the app role every owner right, ownership transfer or not."""
    async with untenanted_session() as s:
        is_member = (
            await s.execute(
                text("SELECT pg_has_role(current_user, 'luciel_audit_owner', 'member')")
            )
        ).scalar_one()
    assert is_member is False


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_audit_insert_still_works_and_the_hash_chain_verifies():
    """The lock must not cost the app its append path: locking the audit log by
    breaking audit writes would be a worse failure than the gap it closes."""
    admin = uuid.uuid4()
    async with tenant_session(admin) as s:
        for i in range(3):
            await audit.emit(
                s,
                admin_id=admin,
                event_type="instance_configured",
                actor_type=ActorType.admin_user,
                payload={"i": i},
            )
    async with tenant_session(admin) as s:
        assert await verify_chain(s, admin) is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_internal_access_insert_still_works_and_is_readable():
    staff = f"ops-{uuid.uuid4().hex[:8]}@example.com"
    async with untenanted_session() as s:
        await emit_internal_access(
            s,
            staff_id=staff,
            approver_id="lead-eng@example.com",
            admin_id=uuid.uuid4(),
            data_class="leads",
            access_started_at=datetime.now(UTC),
            access_type=ACCESS_TYPE_BREAK_GLASS,
            justification="B4 regression coverage",
            ticket_ref="#B4",
        )
    async with untenanted_session() as s:
        count = (
            await s.execute(
                select(func.count())
                .select_from(InternalAccessLog)
                .where(InternalAccessLog.staff_id == staff)
            )
        ).scalar_one()
    assert count == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize("table", AUDIT_TABLES)
async def test_a_statement_level_truncate_guard_exists(table: str):
    """Row-level triggers never fire on TRUNCATE, which is why the row guard alone left
    the hole. Assert the guard is statement-level and armed for TRUNCATE (pg_trigger
    tgtype: bit 0 = ROW, bit 5 (32) = TRUNCATE)."""
    async with untenanted_session() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT t.tgtype FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                    "WHERE c.relname = :t AND NOT t.tgisinternal AND t.tgenabled <> 'D'"
                ),
                {"t": table},
            )
        ).scalars()
        tgtypes = list(rows)
    truncate_guards = [ty for ty in tgtypes if ty & 32 and not ty & 1]
    assert truncate_guards, f"{table} has no enabled statement-level TRUNCATE guard"
