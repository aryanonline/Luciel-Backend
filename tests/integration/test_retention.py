"""Age-based data-retention sweep (Architecture §3.4.10 / §4.4 / Decision #38, P0-4).

Runs against the real DB with RLS. Covers: rows older than the 1-year boundary are
hard-deleted and a data_retention_hard_delete audit event is emitted per class; rows
within the window survive; the sweep is idempotent; and it is tenant-scoped (never
touches another tenant's rows — reuses the isolation harness / assert_no_bypass).

The second half covers the OPT-IN lead auto-prune rule that rides the same sweep. It is a
different policy with a different owner: Legal §B5 makes captured Leads the Admin's own
business data, so the platform prunes them only for a tenant that set a window itself.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.audit.log import verify_chain
from app.db.session import tenant_session
from app.lifecycle.retention import expire_aged_data, prune_stale_leads, run_retention_sweep
from app.models.audit import AdminAuditLog
from app.models.conversation import Session, SessionSummary, Transcript
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.lead import Lead, LeadState
from sqlalchemy import func, select

pytestmark = [pytest.mark.asyncio]


async def _seed_tenant(admin_id: uuid.UUID) -> uuid.UUID:
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
        iid = uuid.uuid4()
        s.add(
            Instance(
                id=iid,
                admin_id=admin_id,
                name="L",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
        return iid


async def _add_transcript(s, *, admin_id, instance_id, created_at, text="hi") -> None:
    s.add(
        Transcript(
            id=uuid.uuid4(),
            admin_id=admin_id,
            instance_id=instance_id,
            session_id=uuid.uuid4(),
            role="lead",
            text=text,
            created_at=created_at,
        )
    )


async def _add_summary(s, *, admin_id, instance_id, created_at, summary="s") -> None:
    s.add(
        SessionSummary(
            id=uuid.uuid4(),
            admin_id=admin_id,
            instance_id=instance_id,
            resolved_lead_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            summary=summary,
            created_at=created_at,
        )
    )


async def _add_session(s, *, admin_id, instance_id, created_at) -> None:
    """The parent conversation row. ``summary`` is conversation content in its own
    right, so it is on the same 1-year clock as the transcripts underneath it."""
    s.add(
        Session(
            id=uuid.uuid4(),
            admin_id=admin_id,
            instance_id=instance_id,
            channel="web",
            summary="caller asked about pricing",
            created_at=created_at,
            started_at=created_at,
            last_message_at=created_at,
        )
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_expire_aged_data_deletes_old_keeps_recent_and_audits():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    now = datetime.now(UTC)
    old = now - timedelta(days=400)  # past the 365d boundary
    recent = now - timedelta(days=10)  # inside the window
    async with tenant_session(admin) as s:
        await _add_transcript(s, admin_id=admin, instance_id=iid, created_at=old)
        await _add_transcript(s, admin_id=admin, instance_id=iid, created_at=recent)
        await _add_summary(s, admin_id=admin, instance_id=iid, created_at=old)
        await _add_summary(s, admin_id=admin, instance_id=iid, created_at=recent)
        await _add_session(s, admin_id=admin, instance_id=iid, created_at=old)
        await _add_session(s, admin_id=admin, instance_id=iid, created_at=recent)
        await s.flush()

    async with tenant_session(admin) as s:
        counts = await expire_aged_data(s, admin_id=admin, now=now)
        assert counts == {"transcript": 1, "session_summary": 1, "session": 1}

    async with tenant_session(admin) as s:
        # Exactly the recent rows survive.
        t = (await s.execute(select(func.count()).select_from(Transcript))).scalar_one()
        ss = (await s.execute(select(func.count()).select_from(SessionSummary))).scalar_one()
        sess = (await s.execute(select(func.count()).select_from(Session))).scalar_one()
        assert t == 1 and ss == 1 and sess == 1
        # A data_retention_hard_delete event per class, and the chain stays intact.
        evts = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "data_retention_hard_delete",
                    )
                )
            )
            .scalars()
            .all()
        )
        classes = {(e.event_payload or {}).get("data_class") for e in evts}
        assert {"transcript", "session_summary", "session"} <= classes
        assert await verify_chain(s, admin) is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_expire_aged_data_is_idempotent():
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    now = datetime.now(UTC)
    old = now - timedelta(days=400)
    async with tenant_session(admin) as s:
        await _add_transcript(s, admin_id=admin, instance_id=iid, created_at=old)
        await _add_summary(s, admin_id=admin, instance_id=iid, created_at=old)
        await _add_session(s, admin_id=admin, instance_id=iid, created_at=old)
        await s.flush()

    async with tenant_session(admin) as s:
        first = await expire_aged_data(s, admin_id=admin, now=now)
        assert first == {"transcript": 1, "session_summary": 1, "session": 1}
    # Re-run: nothing left aged → zero deletes (idempotent).
    async with tenant_session(admin) as s:
        second = await expire_aged_data(s, admin_id=admin, now=now)
        assert second == {"transcript": 0, "session_summary": 0, "session": 0}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_retention_sweep_is_tenant_scoped():
    """The sweep on tenant A must NEVER touch tenant B's aged rows (RLS + app filter)."""
    admin_a, admin_b = uuid.uuid4(), uuid.uuid4()
    iid_a = await _seed_tenant(admin_a)
    iid_b = await _seed_tenant(admin_b)
    now = datetime.now(UTC)
    old = now - timedelta(days=400)
    async with tenant_session(admin_a) as s:
        await _add_transcript(s, admin_id=admin_a, instance_id=iid_a, created_at=old)
        await s.flush()
    async with tenant_session(admin_b) as s:
        await _add_transcript(s, admin_id=admin_b, instance_id=iid_b, created_at=old)
        await s.flush()

    # Sweep ONLY tenant A.
    swept = await run_retention_sweep([admin_a], now=now)
    assert str(admin_a) in swept

    # Tenant A's aged transcript is gone; tenant B's is untouched.
    async with tenant_session(admin_a) as s:
        assert (await s.execute(select(func.count()).select_from(Transcript))).scalar_one() == 0
    async with tenant_session(admin_b) as s:
        assert (await s.execute(select(func.count()).select_from(Transcript))).scalar_one() == 1


# --- the Admin's OPT-IN lead auto-prune rule (§3.4.10a, Legal §B5) ----------- #


async def _add_lead(
    s, *, admin_id, instance_id, last_activity_at, state=LeadState.active
) -> uuid.UUID:
    lid = uuid.uuid4()
    s.add(
        Lead(
            id=lid,
            admin_id=admin_id,
            instance_id=instance_id,
            name="L",
            contact_identifier="l@x.com",
            state=state,
            last_activity_at=last_activity_at,
        )
    )
    return lid


async def _set_retention(admin_id: uuid.UUID, days: int | None) -> None:
    async with tenant_session(admin_id) as s:
        inst = (await s.execute(select(Instance).where(Instance.admin_id == admin_id))).scalar_one()
        inst.lead_retention_days = days
        await s.flush()


async def _lead_ids(admin_id: uuid.UUID) -> set[uuid.UUID]:
    async with tenant_session(admin_id) as s:
        return set((await s.execute(select(Lead.id))).scalars().all())


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_lead_auto_prune_deletes_inactive_and_keeps_active():
    """Staleness is INACTIVITY, not age (§3.4.10a says "inactive for N months"), so the
    cutoff is measured against last_activity_at — a long-running lead that is still being
    worked must survive a window shorter than its own age."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    now = datetime.now(UTC)
    await _set_retention(admin, 30)

    async with tenant_session(admin) as s:
        stale = await _add_lead(
            s, admin_id=admin, instance_id=iid, last_activity_at=now - timedelta(days=45)
        )
        fresh = await _add_lead(
            s, admin_id=admin, instance_id=iid, last_activity_at=now - timedelta(days=5)
        )
        await s.flush()

    async with tenant_session(admin) as s:
        assert await prune_stale_leads(s, admin_id=admin, now=now) == 1

    assert await _lead_ids(admin) == {fresh}
    assert stale not in await _lead_ids(admin)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_no_rule_prunes_nothing():
    """NULL is off and off is the DEFAULT: captured Leads are the Admin's own business
    data, so the platform never timer-deletes them unless the Admin asked it to (§B5)."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    now = datetime.now(UTC)

    async with tenant_session(admin) as s:
        await _add_lead(
            s, admin_id=admin, instance_id=iid, last_activity_at=now - timedelta(days=4000)
        )
        await s.flush()

    async with tenant_session(admin) as s:
        assert await prune_stale_leads(s, admin_id=admin, now=now) == 0
    assert len(await _lead_ids(admin)) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_archived_leads_are_exempt_from_the_prune():
    """§B5 offers archive as the ALTERNATIVE to deletion, so archiving a lead cannot be a
    slower route to losing it."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    now = datetime.now(UTC)
    await _set_retention(admin, 30)

    async with tenant_session(admin) as s:
        archived = await _add_lead(
            s,
            admin_id=admin,
            instance_id=iid,
            last_activity_at=now - timedelta(days=400),
            state=LeadState.archived,
        )
        await s.flush()

    async with tenant_session(admin) as s:
        assert await prune_stale_leads(s, admin_id=admin, now=now) == 0
    assert await _lead_ids(admin) == {archived}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_auto_prune_audits_by_hashed_id_and_is_idempotent():
    """The audit trail must record the deletion without resurrecting the identifier it
    just destroyed (§3.4.11 / §5.3), which is why the id is hashed."""
    from app.audit.log import hash_lead_id

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    now = datetime.now(UTC)
    await _set_retention(admin, 30)

    async with tenant_session(admin) as s:
        stale = await _add_lead(
            s, admin_id=admin, instance_id=iid, last_activity_at=now - timedelta(days=45)
        )
        await s.flush()

    async with tenant_session(admin) as s:
        assert await prune_stale_leads(s, admin_id=admin, now=now) == 1
    # Nothing stale left → a second sweep is a no-op.
    async with tenant_session(admin) as s:
        assert await prune_stale_leads(s, admin_id=admin, now=now) == 0

    async with tenant_session(admin) as s:
        evts = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "lead_pruned",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(evts) == 1
        payload = evts[0].event_payload or {}
        assert payload["resolved_lead_id_hash"] == hash_lead_id(stale)
        assert str(stale) not in json.dumps(payload)
        assert payload["retention_policy"] == "lead_auto_prune:30d"
        assert await verify_chain(s, admin) is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_retention_sweep_applies_each_tenant_s_own_rule():
    """One sweep, two policies: the platform-mandated transcript expiry runs for everyone,
    while the lead prune runs only for the tenant that opted in."""
    admin_on, admin_off = uuid.uuid4(), uuid.uuid4()
    iid_on = await _seed_tenant(admin_on)
    iid_off = await _seed_tenant(admin_off)
    now = datetime.now(UTC)
    await _set_retention(admin_on, 30)

    for admin, iid in ((admin_on, iid_on), (admin_off, iid_off)):
        async with tenant_session(admin) as s:
            await _add_lead(
                s, admin_id=admin, instance_id=iid, last_activity_at=now - timedelta(days=45)
            )
            await s.flush()

    swept = await run_retention_sweep([admin_on, admin_off], now=now)
    assert swept[str(admin_on)]["lead"] == 1
    assert str(admin_off) not in swept  # nothing swept at all for the opted-out tenant

    assert await _lead_ids(admin_on) == set()
    assert len(await _lead_ids(admin_off)) == 1
