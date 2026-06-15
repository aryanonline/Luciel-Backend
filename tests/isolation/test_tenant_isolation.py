"""Tenant-isolation suite — NON-OVERRIDABLE (Architecture §5.15, Space Instr §8).

Proves, against a real Postgres with the app connecting as a non-superuser,
non-BYPASSRLS role, that tenant A can never read or write tenant B's rows across
EVERY tenant table, exercising the three-layer defense (Architecture §3.7.2b):

  Layer 1 — app filter (repositories add WHERE admin_id)
  Layer 2 — Postgres RLS (proven by issuing a NO-WHERE-CLAUSE query and showing it
            still returns only the request tenant's rows — i.e. a forgotten Layer-1
            filter cannot leak)
  Layer 3 — per-request app.admin_id set once from the authenticated subject

These tests are the gate: an isolation failure blocks all merges.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.lead import Lead
from sqlalchemy import func, select, text

pytestmark = [pytest.mark.isolation, pytest.mark.asyncio]


async def _seed_account(session, admin_id: uuid.UUID, email: str) -> Account:
    acct = Account(
        id=admin_id,
        admin_id=admin_id,
        user_id=uuid.uuid4(),
        email=email,
        email_lower=email.lower(),
        password_hash="x",
        state=AccountState.verified,
        email_verified=True,
    )
    session.add(acct)
    await session.flush()
    return acct


async def _seed_instance(session, admin_id: uuid.UUID) -> Instance:
    inst = Instance(id=uuid.uuid4(), admin_id=admin_id, name="L", state=LucielState.active)
    session.add(inst)
    await session.flush()
    return inst


async def _seed_lead(session, admin_id: uuid.UUID, instance_id: uuid.UUID) -> Lead:
    lead = Lead(id=uuid.uuid4(), admin_id=admin_id, instance_id=instance_id, name="Lead")
    session.add(lead)
    await session.flush()
    return lead


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_tenant_cannot_read_other_tenant_rows(two_tenants):
    a, b = two_tenants
    # Tenant A writes its data.
    async with tenant_session(a) as s:
        await _seed_account(s, a, "a@x.com")
        inst_a = await _seed_instance(s, a)
        await _seed_lead(s, a, inst_a.id)
    # Tenant B writes its data.
    async with tenant_session(b) as s:
        await _seed_account(s, b, "b@x.com")
        inst_b = await _seed_instance(s, b)
        await _seed_lead(s, b, inst_b.id)

    # Tenant A's session must see ONLY A's rows — across every tenant table.
    async with tenant_session(a) as s:
        for model in (Account, Instance, Lead, AdminAuditLog):
            rows = (await s.execute(select(model))).scalars().all()
            for row in rows:
                assert row.admin_id == a, f"{model.__name__} leaked tenant B row to A"

    # And symmetrically for B.
    async with tenant_session(b) as s:
        for model in (Account, Instance, Lead):
            rows = (await s.execute(select(model))).scalars().all()
            for row in rows:
                assert row.admin_id == b


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_layer2_rls_holds_without_app_filter(two_tenants):
    """Layer 2 proof: a query with NO admin_id WHERE clause (simulating a forgotten
    Layer-1 filter) still returns only the request tenant's rows."""
    a, b = two_tenants
    async with tenant_session(a) as s:
        await _seed_account(s, a, "a2@x.com")
    async with tenant_session(b) as s:
        await _seed_account(s, b, "b2@x.com")

    # Raw count with no WHERE admin_id — RLS must still scope it.
    async with tenant_session(a) as s:
        total = (await s.execute(select(func.count()).select_from(Account))).scalar_one()
        assert total == 1, "RLS failed: no-filter query saw more than the tenant's own rows"
        only = (await s.execute(select(Account.admin_id))).scalars().all()
        assert set(only) == {a}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_tenant_cannot_write_into_another_tenant(two_tenants):
    """Layer 2 WITH CHECK: inserting a row carrying a different admin_id than the
    request tenant must be rejected by the RLS policy."""
    a, b = two_tenants
    async with tenant_session(a) as s:
        # Try to smuggle a row for tenant B while the request tenant is A.
        rogue = Account(
            id=uuid.uuid4(),
            admin_id=b,  # mismatched on purpose
            user_id=uuid.uuid4(),
            email="rogue@x.com",
            email_lower="rogue@x.com",
            password_hash="x",
            state=AccountState.verified,
            email_verified=True,
        )
        s.add(rogue)
        with pytest.raises(Exception) as exc:
            await s.flush()
        assert "policy" in str(exc.value).lower() or "row-level" in str(exc.value).lower()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_update_cannot_cross_tenant(two_tenants):
    """An UPDATE issued under tenant A cannot modify tenant B's row (RLS hides it)."""
    a, b = two_tenants
    async with tenant_session(b) as s:
        acct_b = await _seed_account(s, b, "bupd@x.com")
        b_id = acct_b.id

    async with tenant_session(a) as s:
        result = await s.execute(
            text("UPDATE accounts SET email = 'hacked' WHERE id = :id"), {"id": str(b_id)}
        )
        assert result.rowcount == 0, "tenant A updated tenant B's row — RLS breach"

    # Confirm B's row is intact.
    async with tenant_session(b) as s:
        email = (
            await s.execute(text("SELECT email FROM accounts WHERE id = :id"), {"id": str(b_id)})
        ).scalar_one()
        assert email == "bupd@x.com"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_delete_cannot_cross_tenant(two_tenants):
    a, b = two_tenants
    async with tenant_session(b) as s:
        lead_inst = await _seed_instance(s, b)
        await _seed_account(s, b, "bdel@x.com")
        lead = await _seed_lead(s, b, lead_inst.id)
        lead_id = lead.id

    async with tenant_session(a) as s:
        result = await s.execute(text("DELETE FROM leads WHERE id = :id"), {"id": str(lead_id)})
        assert result.rowcount == 0, "tenant A deleted tenant B's lead — RLS breach"

    async with tenant_session(b) as s:
        cnt = (
            await s.execute(text("SELECT count(*) FROM leads WHERE id = :id"), {"id": str(lead_id)})
        ).scalar_one()
        assert cnt == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_app_admin_id_required_for_any_tenant_access():
    """Layer 3: with no app.admin_id set, RLS returns zero rows (safe default)."""
    from app.db.engine import get_sessionmaker

    sm = get_sessionmaker()
    # Seed something as a real tenant first.
    tid = uuid.uuid4()
    async with tenant_session(tid) as s:
        await _seed_account(s, tid, "novar@x.com")

    # Now query WITHOUT setting app.admin_id. set_config default is empty → cast fails
    # or returns nothing; RLS must not expose rows.
    async with sm() as s:
        async with s.begin():
            # current_setting('app.admin_id', true) → NULL when unset; comparison is NULL → no rows
            rows = (await s.execute(select(Account))).scalars().all()
            assert rows == [], "rows visible without app.admin_id — Layer 3 breach"
