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
async def test_human_reply_requires_takeover_first(client):
    """§3.4.12: outside human_controlled, Luciel owns the reply. Letting the admin
    interleave would make the transcript lie about who is speaking."""
    admin = await _signup_verify_login(client)
    _iid, sid, _mid = await _seed_conversation(admin)
    r = await client.post(
        f"/api/v1/dashboard/conversations/{sid}/messages", json={"text": "Hi, Ana here."}
    )
    assert r.status_code == 422, r.text


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_human_reply_persists_and_reports_widget_poll_only(client):
    """§11.5-11.7: the admin could take a conversation over but had no way to speak.

    The reply is persisted as a human_agent turn and appears in the transcript. The
    widget has no outbound push transport, so the result must say so honestly rather
    than claim a send the visitor never received.
    """
    from app.models.audit import AdminAuditLog

    admin = await _signup_verify_login(client)
    _iid, sid, _mid = await _seed_conversation(admin)
    await client.post(f"/api/v1/dashboard/conversations/{sid}/take-over")

    r = await client.post(
        f"/api/v1/dashboard/conversations/{sid}/messages",
        json={"text": "Hi, Ana here - happy to help with pricing."},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["message"]["role"] == "human_agent"
    assert body["message"]["text"] == "Hi, Ana here - happy to help with pricing."
    assert body["message"]["at"]
    assert body["delivered"] is False
    assert body["deliveryDetail"] == "widget_poll_only"

    msgs = await client.get(f"/api/v1/dashboard/conversations/{sid}/messages")
    assert [m["role"] for m in msgs.json()] == ["luciel", "human_agent"]

    # The audit trail records the delivery outcome but never the message text (§3.4.11).
    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.event_type == "human_agent_message_sent"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    payload = rows[0].event_payload
    assert payload["delivered"] is False
    assert "Ana here" not in str(payload)


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
async def test_lead_lifecycle_audit_payloads_carry_no_raw_lead_id(client):
    """B2 regression (Architecture §3.4.11 / §5.3): lead_erasure / lead_pruned /
    lead_archived must store the HASHED resolved_lead_id in the immutable chained
    payload, never the raw lead UUID — otherwise PIPEDA erasure-by-key-destruction
    is defeated by the audit log retaining the identifier."""
    from app.audit.log import hash_lead_id
    from app.models.audit import AdminAuditLog

    admin = await _signup_verify_login(client)
    archived = await _seed_lead(admin)
    erased = await _seed_lead(admin)
    await client.post(f"/api/v1/dashboard/leads/{archived}/archive")
    await client.delete(f"/api/v1/dashboard/leads/{erased}")

    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.event_type.in_(["lead_archived", "lead_erasure"])
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows, "expected lead lifecycle audit rows"
    for row in rows:
        blob = str(row.event_payload)
        assert str(archived) not in blob and str(erased) not in blob, (
            "raw lead UUID leaked into immutable audit payload"
        )
        assert "resolved_lead_id_hash" in row.event_payload, "hashed id must be present"
    # The hash for the erased lead is present and matches the one-way digest.
    erasure = [r for r in rows if r.event_type == "lead_erasure"][0]
    assert erasure.event_payload["resolved_lead_id_hash"] == hash_lead_id(erased)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_prune_deletes_named_leads(client):
    admin = await _signup_verify_login(client)
    a, b = await _seed_lead(admin), await _seed_lead(admin)
    r = await client.post("/api/v1/dashboard/leads/prune", json={"leadIds": [str(a), str(b)]})
    assert r.status_code == 204
    async with tenant_session(admin) as s:
        rows = (await s.execute(select(Lead))).scalars().all()
        assert rows == []  # prune = permanent delete (§3.4.10a)


# ---------------------------------------------------------------------------
# Regression tests for Axis3-F01 (lead erasure cascade)
# ---------------------------------------------------------------------------


async def _seed_session_with_transcript(admin: uuid.UUID, lead_id: uuid.UUID) -> uuid.UUID:
    """Seed a session + transcript for the given lead."""
    from app.models.conversation import SessionSummary

    # Use a synthetic iid (no FK to instances required — matches _seed_conversation pattern)
    iid = uuid.uuid4()
    sess_id = uuid.uuid4()

    async with tenant_session(admin) as s:
        s.add(
            SessionModel(
                id=sess_id,
                admin_id=admin,
                instance_id=iid,
                resolved_lead_id=lead_id,
                channel="widget",
                mode="ai",
                counted=False,
            )
        )
        s.add(
            Transcript(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                session_id=sess_id,
                role="user",
                text="hello",
            )
        )
        s.add(
            SessionSummary(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                resolved_lead_id=lead_id,
                session_id=sess_id,
                summary="test summary",
            )
        )
        await s.flush()
        return sess_id


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_erase_lead_cascades_to_transcripts_and_summaries(client):
    """Axis3-F01: erase_lead must delete transcripts and session summaries."""
    from app.models.conversation import SessionSummary

    admin = await _signup_verify_login(client)
    lead_id = await _seed_lead(admin)
    sess_id = await _seed_session_with_transcript(admin, lead_id)

    # Verify precondition: transcript and summary exist
    async with tenant_session(admin) as s:
        transcripts_before = (
            (await s.execute(select(Transcript).where(Transcript.session_id == sess_id)))
            .scalars()
            .all()
        )
        summaries_before = (
            (
                await s.execute(
                    select(SessionSummary).where(SessionSummary.resolved_lead_id == lead_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(transcripts_before) == 1, "precondition: transcript must exist"
    assert len(summaries_before) == 1, "precondition: summary must exist"

    r = await client.delete(f"/api/v1/dashboard/leads/{lead_id}")
    assert r.status_code == 204

    async with tenant_session(admin) as s:
        transcripts_after = (
            (await s.execute(select(Transcript).where(Transcript.session_id == sess_id)))
            .scalars()
            .all()
        )
        summaries_after = (
            (
                await s.execute(
                    select(SessionSummary).where(SessionSummary.resolved_lead_id == lead_id)
                )
            )
            .scalars()
            .all()
        )
    assert transcripts_after == [], "Transcripts must be deleted when lead is erased (§3.4.11)"
    assert summaries_after == [], "SessionSummaries must be deleted when lead is erased (§3.4.11)"
