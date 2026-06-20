"""Lifecycle state machine + cascade integration tests (Architecture §3.6).

Run against the real DB with RLS. Cover: pause/resume, fused-quiesce delete →
grace window, restore → ACTIVE (not paused), the ordered hard-delete cascade
(account shell + audit log survive), and account closure.
"""

from __future__ import annotations

import uuid

import pytest
from app.audit.log import verify_chain
from app.db.session import tenant_session
from app.lifecycle import cascade, state_machine
from app.lifecycle.state_machine import LifecycleError
from app.models.audit import AdminAuditLog
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.knowledge import KnowledgeSource
from app.models.lead import Lead
from sqlalchemy import func, select

pytestmark = [pytest.mark.asyncio]


async def _seed_account_with_luciel(admin_id: uuid.UUID) -> uuid.UUID:
    async with tenant_session(admin_id) as s:
        acct = Account(
            id=admin_id,
            admin_id=admin_id,
            user_id=admin_id,
            email=f"{admin_id}@x.com",
            email_lower=f"{admin_id}@x.com",
            password_hash="x",
            state=AccountState.verified,
            email_verified=True,
        )
        s.add(acct)
        inst = Instance(
            id=uuid.uuid4(),
            admin_id=admin_id,
            name="L",
            state=LucielState.active,
            embed_key_public_id="vm_live_seed",
            channels=[],
            tools=[],
            escalation={},
            personality={},
        )
        s.add(inst)
        s.add(Lead(id=uuid.uuid4(), admin_id=admin_id, instance_id=inst.id, name="Lead"))
        s.add(
            KnowledgeSource(
                id=uuid.uuid4(),
                admin_id=admin_id,
                instance_id=inst.id,
                name="f.pdf",
                origin="upload",
                ingestion_status="ready",
            )
        )
        await s.flush()
        return inst.id


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_pause_resume_roundtrip():
    admin = uuid.uuid4()
    await _seed_account_with_luciel(admin)
    async with tenant_session(admin) as s:
        inst = await state_machine.pause(s, admin, admin)
        assert inst.state == LucielState.paused
    async with tenant_session(admin) as s:
        inst = await state_machine.resume(s, admin, admin)
        assert inst.state == LucielState.active


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_delete_fused_quiesce_to_grace_then_restore_active():
    admin = uuid.uuid4()
    await _seed_account_with_luciel(admin)
    # Delete from ACTIVE (no manual pause-first).
    async with tenant_session(admin) as s:
        inst = await state_machine.delete_luciel(s, admin, admin)
        assert inst.state == LucielState.luciel_grace_window
        assert inst.grace_window_started_at is not None
        assert inst.embed_key_public_id is None  # quiesce revoked the embed key
    # Knowledge soft-deleted during quiesce.
    async with tenant_session(admin) as s:
        src = (
            await s.execute(select(KnowledgeSource).where(KnowledgeSource.admin_id == admin))
        ).scalar_one()
        assert src.soft_deleted_at is not None
    # Restore → ACTIVE (not paused), fresh embed key.
    async with tenant_session(admin) as s:
        inst = await state_machine.restore(s, admin, admin)
        assert inst.state == LucielState.active
        assert inst.grace_window_started_at is None
        assert inst.embed_key_public_id and inst.embed_key_public_id.startswith("vm_live_")
        src = (
            await s.execute(select(KnowledgeSource).where(KnowledgeSource.admin_id == admin))
        ).scalar_one()
        assert src.soft_deleted_at is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_delete_allowed_from_paused():
    admin = uuid.uuid4()
    await _seed_account_with_luciel(admin)
    async with tenant_session(admin) as s:
        await state_machine.pause(s, admin, admin)
    async with tenant_session(admin) as s:
        inst = await state_machine.delete_luciel(s, admin, admin)
        assert inst.state == LucielState.luciel_grace_window


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_restore_only_from_grace_window():
    admin = uuid.uuid4()
    await _seed_account_with_luciel(admin)
    async with tenant_session(admin) as s:
        with pytest.raises(LifecycleError):
            await state_machine.restore(s, admin, admin)  # active, not in grace


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_hard_delete_cascade_preserves_account_shell_and_audit():
    admin = uuid.uuid4()
    await _seed_account_with_luciel(admin)
    async with tenant_session(admin) as s:
        await state_machine.delete_luciel(s, admin, admin)
    async with tenant_session(admin) as s:
        counts = await cascade.hard_delete_cascade(s, admin)
        assert counts["leads"] >= 1
        assert counts["knowledge_sources"] >= 1
    # Instance + leads gone; account shell + audit log survive; chain intact.
    async with tenant_session(admin) as s:
        assert (await s.execute(select(func.count()).select_from(Instance))).scalar_one() == 0
        assert (await s.execute(select(func.count()).select_from(Lead))).scalar_one() == 0
        acct = (await s.execute(select(Account).where(Account.id == admin))).scalar_one()
        assert acct is not None  # shell survives
        audit_count = (
            await s.execute(select(func.count()).select_from(AdminAuditLog))
        ).scalar_one()
        assert audit_count > 0  # tombstones retained (proof of deletion)
        assert await verify_chain(s, admin) is True  # hash chain intact through deletions


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_close_account_deletes_luciel_first_then_closes():
    admin = uuid.uuid4()
    await _seed_account_with_luciel(admin)
    async with tenant_session(admin) as s:
        await cascade.close_account(s, admin, admin)
    async with tenant_session(admin) as s:
        acct = (await s.execute(select(Account).where(Account.id == admin))).scalar_one()
        assert acct.state == AccountState.closed
        # Luciel was deleted as part of the nested close flow.
        assert (await s.execute(select(func.count()).select_from(Instance))).scalar_one() == 0
        assert await verify_chain(s, admin) is True


# ---------------------------------------------------------------------------
# Regression tests for Axis3-F02 (hard-delete cascade calls SecretStore.delete)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_hard_delete_cascade_calls_secret_store_delete():
    """Axis3-F02: hard_delete_cascade must call SecretStore.delete() for each connection."""
    import uuid as _uuid

    from app.connections.secrets import LocalSecretStore, secret_ref_for
    from app.lifecycle.cascade import hard_delete_cascade
    from app.models.connection import InstanceConnection

    admin = _uuid.uuid4()
    store = LocalSecretStore()
    # Patch the global store so the cascade uses our test store.
    import app.connections.secrets as _sec

    original = _sec._default
    _sec._default = store

    try:
        async with tenant_session(admin) as s:
            s.add(
                Account(
                    id=admin,
                    admin_id=admin,
                    user_id=admin,
                    email=f"{admin}@cs.test",
                    email_lower=f"{admin}@cs.test",
                    password_hash="x",
                    state=AccountState.verified,
                    email_verified=True,
                )
            )
            iid = _uuid.uuid4()
            s.add(
                Instance(
                    id=iid,
                    admin_id=admin,
                    name="T",
                    state=LucielState.luciel_grace_window,
                    channels=[],
                    tools=[],
                    escalation={},
                    personality={},
                )
            )
            ref = secret_ref_for(admin, iid, "calendar")
            s.add(
                InstanceConnection(
                    connection_id=_uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    connection_type="calendar",
                    auth_class="oauth_token",
                    provider="google",
                    status="connected",
                    secret_ref=ref,
                )
            )
            await s.flush()

        # Seed a secret into the store.
        await store.put(ref, {"access_token": "tok-abc"})
        assert await store.get(ref) is not None, "precondition: secret must be in store"

        async with tenant_session(admin) as s:
            await hard_delete_cascade(s, admin)

        # Secret must have been deleted by the cascade.
        assert await store.get(ref) is None, (
            "SecretStore.delete() must be called during hard_delete_cascade (§3.6.5 step 6)"
        )
    finally:
        _sec._default = original


# ---------------------------------------------------------------------------
# Regression tests for Axis3-F03 (lead_reactivated event)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_archived_lead_reactivated_on_inbound():
    """Axis3-F03: Returning archived lead is reactivated with lead_reactivated event."""
    from app.models.audit import AdminAuditLog
    from app.models.lead import LeadState
    from app.runtime.sessions import resolve_or_open_session

    admin = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin}@lr.test",
                email_lower=f"{admin}@lr.test",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        iid = uuid.uuid4()
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="T",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        lead_id = uuid.uuid4()
        from app.models.lead import Lead

        s.add(
            Lead(
                id=lead_id,
                admin_id=admin,
                instance_id=iid,
                name="Test User",
                contact_identifier="test@x.com",
                state=LeadState.archived,
            )
        )
        await s.flush()

    async with tenant_session(admin) as s:
        sess, is_new = await resolve_or_open_session(
            s,
            admin_id=admin,
            instance_id=iid,
            channel="email",
            resolved_lead_id=lead_id,
        )

    # Lead must now be active
    async with tenant_session(admin) as s:
        from app.models.lead import Lead as LeadModel
        from sqlalchemy import select as sa_sel

        lead = (await s.execute(sa_sel(LeadModel).where(LeadModel.id == lead_id))).scalar_one()
        assert lead.state == LeadState.active, (
            f"Archived lead must be reactivated on inbound; got {lead.state!r}"
        )

        # lead_reactivated audit event must be emitted
        evt = (
            await s.execute(
                sa_sel(AdminAuditLog).where(
                    AdminAuditLog.admin_id == admin,
                    AdminAuditLog.event_type == "lead_reactivated",
                )
            )
        ).scalar_one_or_none()
        assert evt is not None, "lead_reactivated audit event must be emitted"
