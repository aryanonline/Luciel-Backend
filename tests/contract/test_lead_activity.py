"""Owner actions are activity (§3.4.10a) — 2026-09-05 audit F096.

``last_activity_at`` was written only by the lead-capture cognition, so a lead the
owner had just marked Won still read as "inactive" to the auto-prune rule and was
deleted. Archiving and marking an outcome now stamp the clock.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db.session import tenant_session
from app.models.lead import Lead, LeadState
from sqlalchemy import select

pytestmark = [pytest.mark.contract]


async def _signup_verify_login(client) -> uuid.UUID:
    from app.auth import access

    email = f"activity-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "t"},
    )
    acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    return acct.id


async def _seed_stale_lead(admin: uuid.UUID) -> uuid.UUID:
    lead_id = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Lead(
                id=lead_id,
                admin_id=admin,
                instance_id=uuid.uuid4(),
                name="Jane",
                contact_identifier="jane@x.com",
                state=LeadState.active,
                last_activity_at=datetime.now(UTC) - timedelta(days=200),
            )
        )
    return lead_id


async def _activity(admin: uuid.UUID, lead_id: uuid.UUID) -> datetime:
    async with tenant_session(admin) as s:
        lead = (await s.execute(select(Lead).where(Lead.id == lead_id))).scalar_one()
        return lead.last_activity_at


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_marking_an_outcome_is_activity(client) -> None:
    admin = await _signup_verify_login(client)
    lead_id = await _seed_stale_lead(admin)
    r = await client.post(
        f"/api/v1/dashboard/leads/{lead_id}/outcome", json={"outcome": "converted"}
    )
    assert r.status_code == 200, r.text
    assert await _activity(admin, lead_id) > datetime.now(UTC) - timedelta(minutes=1)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_archiving_is_activity(client) -> None:
    admin = await _signup_verify_login(client)
    lead_id = await _seed_stale_lead(admin)
    r = await client.post(f"/api/v1/dashboard/leads/{lead_id}/archive")
    assert r.status_code == 200, r.text
    assert await _activity(admin, lead_id) > datetime.now(UTC) - timedelta(minutes=1)
