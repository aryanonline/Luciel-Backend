"""Age-based data-retention sweep (Architecture §3.4.10 / §4.4 / Decision #38, P0-4).

Runs against the real DB with RLS. Covers: rows older than the 1-year boundary are
hard-deleted and a data_retention_hard_delete audit event is emitted per class; rows
within the window survive; the sweep is idempotent; and it is tenant-scoped (never
touches another tenant's rows — reuses the isolation harness / assert_no_bypass).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.audit.log import verify_chain
from app.db.session import tenant_session
from app.lifecycle.retention import expire_aged_data, run_retention_sweep
from app.models.audit import AdminAuditLog
from app.models.conversation import SessionSummary, Transcript
from app.models.instance import Account, AccountState, Instance, LucielState
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
        await s.flush()

    async with tenant_session(admin) as s:
        counts = await expire_aged_data(s, admin_id=admin, now=now)
        assert counts == {"transcript": 1, "session_summary": 1}

    async with tenant_session(admin) as s:
        # Exactly the recent rows survive.
        t = (await s.execute(select(func.count()).select_from(Transcript))).scalar_one()
        ss = (await s.execute(select(func.count()).select_from(SessionSummary))).scalar_one()
        assert t == 1 and ss == 1
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
        assert {"transcript", "session_summary"} <= classes
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
        await s.flush()

    async with tenant_session(admin) as s:
        first = await expire_aged_data(s, admin_id=admin, now=now)
        assert first == {"transcript": 1, "session_summary": 1}
    # Re-run: nothing left aged → zero deletes (idempotent).
    async with tenant_session(admin) as s:
        second = await expire_aged_data(s, admin_id=admin, now=now)
        assert second == {"transcript": 0, "session_summary": 0}


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
