"""Contract conformance for /api/v1/dashboard/* (conversations + leads).

Mirrors http-admin.ts: nested message evidence/flag paths, camelCase wire shapes,
take-over/hand-back mode transitions, escalations list, lead erase/prune/archive.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.models.conversation import EscalationEvent, Transcript
from app.models.conversation import Session as SessionModel
from app.models.lead import Lead, LeadState
from sqlalchemy import select

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


async def _signup_verify_login(client) -> uuid.UUID:
    from app.auth import access

    email = f"dash-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "t"},
    )
    acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    return acct.id


async def _seed_conversation(admin: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    iid, sid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            SessionModel(
                id=sid,
                admin_id=admin,
                instance_id=iid,
                channel="widget",
                mode="ai",
                summary="Asked about pricing",
            )
        )
        mid = uuid.uuid4()
        s.add(
            Transcript(
                id=mid,
                admin_id=admin,
                instance_id=iid,
                session_id=sid,
                role="luciel",
                text="Our intro call is free.",
            )
        )
        s.add(
            EscalationEvent(
                escalation_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                session_id=sid,
                signal="high_value_lead",
                gate="outcome",
                score_or_confidence=0.7,
            )
        )
        await s.flush()
        return iid, sid, mid


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_list_conversations_camelcase(client):
    admin = await _signup_verify_login(client)
    _iid, sid, _mid = await _seed_conversation(admin)
    r = await client.get("/api/v1/dashboard/conversations")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    row = body[0]
    # camelCase wire keys (WireModel).
    assert row["sessionId"] == str(sid)
    assert row["lastMessageAt"] is not None
    assert row["mode"] == "ai"
    assert row["summary"] == "Asked about pricing"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_messages_and_nested_evidence(client):
    admin = await _signup_verify_login(client)
    _iid, sid, mid = await _seed_conversation(admin)
    rm = await client.get(f"/api/v1/dashboard/conversations/{sid}/messages")
    assert rm.status_code == 200
    assert rm.json()[0]["messageId"] == str(mid)
    # Evidence is NESTED under the message (frozen path).
    re = await client.get(f"/api/v1/dashboard/conversations/{sid}/messages/{mid}/evidence")
    assert re.status_code == 200
    ev = re.json()
    assert ev["messageId"] == str(mid)
    assert ev["groundingScore"] == 0.50  # uniform floor (§3.4.13)
    assert ev["sourceChunks"] == []
    assert ev["flaggedByAdmin"] is False


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_flag_answer_returns_204(client):
    admin = await _signup_verify_login(client)
    _iid, sid, mid = await _seed_conversation(admin)
    rf = await client.post(f"/api/v1/dashboard/conversations/{sid}/messages/{mid}/flag")
    assert rf.status_code == 204


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_take_over_then_hand_back(client):
    admin = await _signup_verify_login(client)
    _iid, sid, _mid = await _seed_conversation(admin)
    rt = await client.post(f"/api/v1/dashboard/conversations/{sid}/take-over")
    assert rt.status_code == 200
    assert rt.json()["mode"] == "human_controlled"
    rh = await client.post(f"/api/v1/dashboard/conversations/{sid}/hand-back")
    assert rh.json()["mode"] == "ai"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_missing_conversation_is_404(client):
    await _signup_verify_login(client)
    r = await client.get(f"/api/v1/dashboard/conversations/{uuid.uuid4()}/messages")
    assert r.status_code == 404


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_escalations_list(client):
    admin = await _signup_verify_login(client)
    _iid, sid, _mid = await _seed_conversation(admin)
    r = await client.get("/api/v1/dashboard/escalations")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["signal"] == "high_value_lead"
    assert body[0]["gate"] == "outcome"
    assert body[0]["scoreOrConfidence"] == 0.7


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_unauthenticated_dashboard_is_401(client):
    # No session cookie → unauthorized (§3.7.2).
    r = await client.get("/api/v1/dashboard/conversations")
    assert r.status_code == 401


# --- leads ------------------------------------------------------------------- #
async def _seed_lead(admin: uuid.UUID) -> uuid.UUID:
    iid, lead_id = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Lead(
                id=lead_id,
                admin_id=admin,
                instance_id=iid,
                name="Jane",
                contact_identifier="jane@x.com",
                intent="quote",
                state=LeadState.active,
            )
        )
        await s.flush()
    return lead_id


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_list_leads_camelcase(client):
    admin = await _signup_verify_login(client)
    lead_id = await _seed_lead(admin)
    r = await client.get("/api/v1/dashboard/leads")
    assert r.status_code == 200
    row = r.json()[0]
    assert row["leadId"] == str(lead_id)
    assert row["contactIdentifier"] == "jane@x.com"
    assert row["state"] == "active"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_archive_lead(client):
    admin = await _signup_verify_login(client)
    lead_id = await _seed_lead(admin)
    r = await client.post(f"/api/v1/dashboard/leads/{lead_id}/archive")
    assert r.status_code == 200
    assert r.json()["state"] == "archived"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_erase_lead_deletes_row(client):
    admin = await _signup_verify_login(client)
    lead_id = await _seed_lead(admin)
    r = await client.delete(f"/api/v1/dashboard/leads/{lead_id}")
    assert r.status_code == 204
    async with tenant_session(admin) as s:
        rows = (await s.execute(select(Lead).where(Lead.id == lead_id))).scalars().all()
        assert rows == []  # hard-deleted (§3.4.11)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_prune_deletes_named_leads(client):
    admin = await _signup_verify_login(client)
    a, b = await _seed_lead(admin), await _seed_lead(admin)
    r = await client.post("/api/v1/dashboard/leads/prune", json={"leadIds": [str(a), str(b)]})
    assert r.status_code == 204
    async with tenant_session(admin) as s:
        rows = (await s.execute(select(Lead))).scalars().all()
        assert rows == []  # prune = permanent delete (§3.4.10a)
