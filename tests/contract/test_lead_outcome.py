"""Lead outcome marking (Vision §7 — C14, migration 0050).

The conversion dimension is admin-marked: in_progress (default) / converted /
lost, reversible, audited without lead PII, and served back on every lead row.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.instance import Instance, LucielState
from app.models.lead import Lead
from sqlalchemy import select

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


async def _ready_account(client) -> uuid.UUID:
    from app.auth import access
    from app.auth.tokens import decode_token
    from app.config import get_settings

    email = f"out-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "pw-123456", "captchaToken": "t"}
    )
    _a, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    return uuid.UUID(decode_token(client.cookies.get(get_settings().session_cookie_name))["sub"])


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_mark_and_reverse_a_lead_outcome(client):
    admin = await _ready_account(client)
    lead_id = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Instance(
                id=admin,
                admin_id=admin,
                name="L",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        s.add(Lead(id=lead_id, admin_id=admin, instance_id=admin, name="Jordan"))
        await s.flush()

    listed = await client.get("/api/v1/dashboard/leads")
    assert listed.json()[0]["outcome"] == "in_progress"  # the honest unmarked default

    r = await client.post(
        f"/api/v1/dashboard/leads/{lead_id}/outcome", json={"outcome": "converted"}
    )
    assert r.status_code == 200
    assert r.json()["outcome"] == "converted"

    # Reversible — the owner corrects a mismark without support intervention.
    r2 = await client.post(f"/api/v1/dashboard/leads/{lead_id}/outcome", json={"outcome": "lost"})
    assert r2.json()["outcome"] == "lost"

    r3 = await client.post(
        f"/api/v1/dashboard/leads/{lead_id}/outcome", json={"outcome": "elsewhere"}
    )
    assert r3.status_code == 422  # only the three wire values exist

    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog).where(AdminAuditLog.event_type == "lead_outcome_marked")
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        # No lead PII in the audit payload (§3.4.11) — hashed id + outcomes only.
        for row in rows:
            assert "Jordan" not in str(row.event_payload)
            assert row.event_payload["new_outcome"] in ("converted", "lost")
