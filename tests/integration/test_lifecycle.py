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
