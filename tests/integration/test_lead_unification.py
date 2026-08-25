"""Round 5A item 7 — capture-time lead unification, and item 2's link hygiene.

The same person returning under a second identity used to become a duplicate
Lead AND a duplicate CRM record with no way back. Unification fires on exact
identifier equality (emails case-folded), keeps the older row, re-points every
resolved_lead_id carrier, and hard-deletes the loser — audited with hashed ids
only. Erase/prune take the lead's CRM link rows (plaintext email) with them.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.conversation import Session as SessionModel
from app.models.crm_link import CrmLeadLink
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.lead import Lead, LeadState
from app.runtime.lead_capture import capture_or_update_lead
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]


async def _seed_tenant(admin: uuid.UUID) -> uuid.UUID:
    iid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin.hex[:8]}@x.com",
                email_lower=f"{admin.hex[:8]}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="U",
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{admin.hex[:10]}",
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
    return iid


async def _seed_lead(
    admin: uuid.UUID,
    iid: uuid.UUID,
    *,
    contact: str,
    name: str | None = None,
    opted_out: bool = False,
    outcome: str = "in_progress",
    created: datetime | None = None,
) -> uuid.UUID:
    lid = uuid.uuid4()
    async with tenant_session(admin) as s:
        lead = Lead(
            id=lid,
            admin_id=admin,
            instance_id=iid,
            name=name,
            contact_identifier=contact,
            state=LeadState.active,
            opted_out=opted_out,
            outcome=outcome,
        )
        if created is not None:
            lead.created_at = created
        s.add(lead)
        await s.flush()
    return lid


async def _seed_session(admin: uuid.UUID, iid: uuid.UUID, lead_id: uuid.UUID | None) -> uuid.UUID:
    sid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            SessionModel(
                id=sid,
                admin_id=admin,
                instance_id=iid,
                channel="widget",
                mode="ai",
                resolved_lead_id=lead_id,
            )
        )
        await s.flush()
    return sid


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_same_identifier_on_a_second_row_unifies_into_the_older_lead():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    older = await _seed_lead(
        admin,
        iid,
        contact="jane@acme.com",
        name="Jane Miller",
        opted_out=True,
        outcome="converted",
        created=datetime(2026, 1, 1, tzinfo=UTC),
    )
    newer = await _seed_lead(admin, iid, contact="j.other@personal.com")
    sid = await _seed_session(admin, iid, newer)
    other_sid = await _seed_session(admin, iid, newer)

    async with tenant_session(admin) as s:
        sess = (await s.execute(select(SessionModel).where(SessionModel.id == sid))).scalar_one()
        result = await capture_or_update_lead(
            s,
            admin_id=admin,
            instance_id=iid,
            sess=sess,
            inbound_text="It's me again — jane@acme.com, still after that quote.",
            channel="widget",
            transport_identifier=None,
        )
        assert result is not None
        assert result.lead_id == older

    async with tenant_session(admin) as s:
        leads = (await s.execute(select(Lead).where(Lead.admin_id == admin))).scalars().all()
        assert [le.id for le in leads] == [older]
        survivor = leads[0]
        assert survivor.name == "Jane Miller"
        assert survivor.opted_out is True  # an opt-out on either identity survives
        assert survivor.outcome == "converted"  # most-advanced outcome kept
        # Every carrier re-pointed — no dangling resolved_lead_id.
        sessions = (
            (await s.execute(select(SessionModel).where(SessionModel.admin_id == admin)))
            .scalars()
            .all()
        )
        assert {x.resolved_lead_id for x in sessions} == {older}
        assert {x.id for x in sessions} == {sid, other_sid}
        merged = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "lead_merged",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(merged) == 1
        payload = merged[0].event_payload or {}
        # Hashed ids only (§3.4.11) — never raw identifiers in the chained log.
        assert "jane@acme.com" not in str(payload)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_unification_never_crosses_tenants():
    admin_a, admin_b = uuid.uuid4(), uuid.uuid4()
    iid_a = await _seed_tenant(admin_a)
    iid_b = await _seed_tenant(admin_b)
    await _seed_lead(admin_b, iid_b, contact="jane@acme.com", name="Tenant B's Jane")
    bound = await _seed_lead(admin_a, iid_a, contact="a-side@x.com")
    sid = await _seed_session(admin_a, iid_a, bound)

    async with tenant_session(admin_a) as s:
        sess = (await s.execute(select(SessionModel).where(SessionModel.id == sid))).scalar_one()
        await capture_or_update_lead(
            s,
            admin_id=admin_a,
            instance_id=iid_a,
            sess=sess,
            inbound_text="reach me at jane@acme.com",
            channel="widget",
            transport_identifier=None,
        )
    async with tenant_session(admin_b) as s:
        b_leads = (await s.execute(select(Lead).where(Lead.admin_id == admin_b))).scalars().all()
        assert len(b_leads) == 1
        assert b_leads[0].name == "Tenant B's Jane"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_erase_takes_the_crm_link_rows_with_it():
    """Round 5A item 2: the link rows carry the person's email in plaintext — an
    erasure that leaves them behind is not an erasure."""
    from app.tools.crm_backend import delete_links_for_lead

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    lid = await _seed_lead(admin, iid, contact="jane@acme.com")
    async with tenant_session(admin) as s:
        s.add(
            CrmLeadLink(
                admin_id=admin,
                instance_id=iid,
                provider="hubspot",
                lead_id=lid,
                lead_key="jane@acme.com",
                crm_record_id="rec-1",
            )
        )
        # A legacy row with no lead_id but the lead's key must go too.
        s.add(
            CrmLeadLink(
                admin_id=admin,
                instance_id=iid,
                provider="salesforce",
                lead_id=None,
                lead_key="jane@acme.com",
                crm_record_id="rec-2",
            )
        )
        await s.flush()
    async with tenant_session(admin) as s:
        lead = (await s.execute(select(Lead).where(Lead.id == lid))).scalar_one()
        deleted = await delete_links_for_lead(s, admin_id=admin, lead=lead)
        assert deleted == 2
        remaining = (
            (await s.execute(select(CrmLeadLink).where(CrmLeadLink.admin_id == admin)))
            .scalars()
            .all()
        )
        assert remaining == []
