"""Contract conformance for /api/v1/dashboard/* (conversations + leads).

Mirrors http-admin.ts: nested message evidence/flag paths, camelCase wire shapes,
take-over/hand-back mode transitions, escalations list, lead erase/prune/archive.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db.session import tenant_session
from app.models.conversation import EscalationEvent, Transcript
from app.models.conversation import Session as SessionModel
from app.models.lead import Lead, LeadState
from sqlalchemy import select

pytestmark = [pytest.mark.contract]


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
    """LIVE-VERIFIED (Harmony wave 2, items 2a/2c): the seeded message here has NO
    grounding score and NO source_chunks persisted (it models a genuine
    pre-migration/legacy Transcript row — created before either column existed).
    The harmony-fix-2026-08-04 version of this test asserted groundingScore == 0.50
    for exactly this row, which is the item-2c bug itself: a legacy row must render
    a "not scored" marker, never the floor constant dressed up as a real score.
    Likewise sourceChunks == [] here must be reported as attribution_unavailable
    (we never captured attribution for this row), NOT no_sources_retrieved (which
    would claim retrieval genuinely ran and found nothing) and NEVER rendered by
    the frontend as "no source backed this answer"."""
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
    assert ev["groundingScore"] is None
    assert ev["scoringStatus"] == "not_scored_legacy"
    assert ev["sourceChunks"] == []
    assert ev["attributionStatus"] == "attribution_unavailable"
    assert ev["flaggedByAdmin"] is False


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_answer_flag_survives_a_reload(client):
    """Round 7 WP-4c #8: the flag was audit-only and the evidence read returned a
    constant False — flag, reload, unflagged. Now persisted on the row (0070)."""
    admin = await _signup_verify_login(client)
    _iid, sid, mid = await _seed_conversation(admin)
    evidence = f"/api/v1/dashboard/conversations/{sid}/messages/{mid}/evidence"
    assert (await client.get(evidence)).json()["flaggedByAdmin"] is False

    rf = await client.post(f"/api/v1/dashboard/conversations/{sid}/messages/{mid}/flag")
    assert rf.status_code == 204
    assert (await client.get(evidence)).json()["flaggedByAdmin"] is True

    async with tenant_session(admin) as s:
        row = (await s.execute(select(Transcript).where(Transcript.id == mid))).scalar_one()
        first = row.flagged_by_admin_at
        assert first is not None
    # Flagging again is harmless and keeps the first timestamp.
    assert (
        await client.post(f"/api/v1/dashboard/conversations/{sid}/messages/{mid}/flag")
    ).status_code == 204
    assert (await client.get(evidence)).json()["flaggedByAdmin"] is True
    async with tenant_session(admin) as s:
        row = (await s.execute(select(Transcript).where(Transcript.id == mid))).scalar_one()
        assert row.flagged_by_admin_at == first
    # A different message in the same session is not flagged by association.
    other = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Transcript(
                id=other,
                admin_id=admin,
                instance_id=_iid,
                session_id=sid,
                role="luciel",
                text="Another answer",
            )
        )
    assert (
        await client.get(f"/api/v1/dashboard/conversations/{sid}/messages/{other}/evidence")
    ).json()["flaggedByAdmin"] is False


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_evidence_deterministic_row_is_not_applicable_not_legacy(client):
    """#5c (live-caught 2026-08-23): a fresh greeting/at-cap/ack row is composed in
    code — scoring never applied to it. It used to serve not_scored_legacy and the
    dashboard claimed the row was written "before scoring existed", false for a row
    minutes old. The 0051 `deterministic` marker splits the two truths on the wire:
    marked rows serve "not_applicable"; unmarked unscored rows (whose origin the
    backend cannot reconstruct) honestly keep "not_scored_legacy"."""
    admin = await _signup_verify_login(client)
    iid, sid, legacy_mid = await _seed_conversation(admin)
    det_id = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Transcript(
                id=det_id,
                admin_id=admin,
                instance_id=iid,
                session_id=sid,
                role="luciel",
                text="Hi! I'm an AI assistant for this business. How can I help?",
                deterministic=True,
            )
        )
        await s.flush()

    re_det = await client.get(f"/api/v1/dashboard/conversations/{sid}/messages/{det_id}/evidence")
    assert re_det.status_code == 200
    ev = re_det.json()
    assert ev["scoringStatus"] == "not_applicable"
    assert ev["groundingScore"] is None
    # The unmarked unscored row from _seed_conversation keeps the legacy status —
    # the marker must never leak onto rows that don't carry it.
    re_legacy = await client.get(
        f"/api/v1/dashboard/conversations/{sid}/messages/{legacy_mid}/evidence"
    )
    assert re_legacy.json()["scoringStatus"] == "not_scored_legacy"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_evidence_returns_the_real_persisted_grounding_score(client):
    """This is the actual harmony fix: two different messages with two different
    REAL scores must return two different values, not both hitting the same
    hardcoded 0.50 the walkthrough observed for every answer."""
    admin = await _signup_verify_login(client)
    iid, sid, _mid = await _seed_conversation(admin)
    high_id, low_id = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Transcript(
                id=high_id,
                admin_id=admin,
                instance_id=iid,
                session_id=sid,
                role="luciel",
                text="Electronics have a 15 day return window per our policy.",
                grounding_score=0.83,
            )
        )
        s.add(
            Transcript(
                id=low_id,
                admin_id=admin,
                instance_id=iid,
                session_id=sid,
                role="luciel",
                text="Sure, we offer a lifetime warranty on all products.",
                grounding_score=0.0,
            )
        )
        await s.flush()

    r_high = await client.get(f"/api/v1/dashboard/conversations/{sid}/messages/{high_id}/evidence")
    r_low = await client.get(f"/api/v1/dashboard/conversations/{sid}/messages/{low_id}/evidence")
    assert r_high.status_code == 200 and r_low.status_code == 200
    assert r_high.json()["groundingScore"] == pytest.approx(0.83)
    assert r_high.json()["scoringStatus"] == "scored"
    assert r_low.json()["groundingScore"] == 0.0
    assert r_low.json()["scoringStatus"] == "scored"
    assert r_high.json()["groundingScore"] != r_low.json()["groundingScore"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_evidence_reports_has_sources_for_real_attributed_chunks(client):
    """LIVE-VERIFIED (Harmony wave 2, item 2a): a reply with REAL persisted
    source_chunks (the retriever had them and Orchestrator.chunk_attributor
    persisted them via persist_turn) must report attributionStatus ==
    "has_sources" and return the real chunk refs — never an empty list dressed
    up as "no source backed this answer"."""
    admin = await _signup_verify_login(client)
    iid, sid, _mid = await _seed_conversation(admin)
    msg_id = uuid.uuid4()
    src_id = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Transcript(
                id=msg_id,
                admin_id=admin,
                instance_id=iid,
                session_id=sid,
                role="luciel",
                text="Standard shipping takes 5 to 7 business days.",
                grounding_score=0.9,
                source_chunks=[
                    {
                        "source_id": str(src_id),
                        "source_name": "Shipping FAQ.pdf",
                        "text": "Standard shipping takes 5 to 7 business days.",
                    }
                ],
            )
        )
        await s.flush()

    r = await client.get(f"/api/v1/dashboard/conversations/{sid}/messages/{msg_id}/evidence")
    assert r.status_code == 200
    body = r.json()
    assert body["attributionStatus"] == "has_sources"
    assert body["scoringStatus"] == "scored"
    assert len(body["sourceChunks"]) == 1
    assert body["sourceChunks"][0]["sourceName"] == "Shipping FAQ.pdf"
    assert body["sourceChunks"][0]["sourceId"] == str(src_id)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_evidence_reports_no_sources_retrieved_when_retrieval_found_nothing(client):
    """An empty source_chunks list that WAS captured (retrieval genuinely ran and
    found nothing) must be distinguished from attribution_unavailable — both
    render as an empty sourceChunks array, but only no_sources_retrieved is an
    honest claim about retrieval outcome."""
    admin = await _signup_verify_login(client)
    iid, sid, _mid = await _seed_conversation(admin)
    msg_id = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Transcript(
                id=msg_id,
                admin_id=admin,
                instance_id=iid,
                session_id=sid,
                role="luciel",
                text="I don't have information on that in my knowledge base.",
                grounding_score=0.0,
                source_chunks=[],
            )
        )
        await s.flush()

    r = await client.get(f"/api/v1/dashboard/conversations/{sid}/messages/{msg_id}/evidence")
    assert r.status_code == 200
    body = r.json()
    assert body["attributionStatus"] == "no_sources_retrieved"
    assert body["sourceChunks"] == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_evidence_reports_retrieval_unavailable_when_the_embedder_failed(client):
    """A turn whose retrieval never RAN (the embedding provider failed) persists
    the one-entry marker, not an empty list — and the wire says so
    (retrieval_unavailable), never "no source backed this answer" over a
    question the tenant's sources do answer (2026-09-06 E2E walk: OpenAI 429 at
    quota turned a plain hours question into a hand-off)."""
    admin = await _signup_verify_login(client)
    iid, sid, _mid = await _seed_conversation(admin)
    msg_id = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Transcript(
                id=msg_id,
                admin_id=admin,
                instance_id=iid,
                session_id=sid,
                role="luciel",
                text="I've flagged this for the team, and they'll follow up with you shortly.",
                grounding_score=0.0,
                source_chunks=[{"unavailable": "openai_error:RateLimitError"}],
            )
        )
        await s.flush()

    r = await client.get(f"/api/v1/dashboard/conversations/{sid}/messages/{msg_id}/evidence")
    assert r.status_code == 200
    body = r.json()
    assert body["attributionStatus"] == "retrieval_unavailable"
    assert body["sourceChunks"] == []


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
async def test_the_transcript_pages_and_keeps_its_shape(client):
    """WP-8 pagination audit: the transcript read was one unbounded query."""
    admin = await _signup_verify_login(client)
    _iid, sid, mid = await _seed_conversation(admin)
    url = f"/api/v1/dashboard/conversations/{sid}/messages"
    assert (await client.get(url, params={"limit": 1})).json()[0]["messageId"] == str(mid)
    assert (await client.get(url, params={"limit": 1, "offset": 1})).json() == []
    assert (await client.get(url, params={"limit": 501})).status_code == 422


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_transcript_past_the_page_ceiling_still_shows_its_newest_messages(client):
    """The takeover view polls this route with no parameters: the customer's next message
    must appear on a session past 500 rows, in order, and the cut must be readable."""
    admin = await _signup_verify_login(client)
    iid, sid, _mid = await _seed_conversation(admin)
    start = datetime.now(UTC) - timedelta(days=30)
    async with tenant_session(admin) as s:
        for n in range(502):
            s.add(
                Transcript(
                    admin_id=admin,
                    instance_id=iid,
                    session_id=sid,
                    role="lead",
                    text=f"message {n}",
                    at=start + timedelta(minutes=n),
                )
            )
    r = await client.get(f"/api/v1/dashboard/conversations/{sid}/messages")
    assert r.status_code == 200
    assert r.headers["X-Total-Count"] == "503"
    texts = [m["text"] for m in r.json()]
    assert len(texts) == 500
    assert texts[-1] == "Our intro call is free."  # the seeded row is the newest: at = now()
    assert texts[-2] == "message 501"
    assert texts[0] == "message 3"
    older = await client.get(
        f"/api/v1/dashboard/conversations/{sid}/messages", params={"offset": 500}
    )
    assert [m["text"] for m in older.json()] == ["message 0", "message 1", "message 2"]


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


# --- lead export (§3.4.10a "exportable", Legal §A7 portability) -------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_export_leads_as_csv_by_default(client):
    admin = await _signup_verify_login(client)
    lead_id = await _seed_lead(admin)
    r = await client.get("/api/v1/dashboard/leads/export")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "leads.csv" in r.headers["content-disposition"]
    header, row = r.text.strip().splitlines()[:2]
    assert header.split(",")[0] == "leadId"
    assert str(lead_id) in row
    assert "jane@x.com" in row


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_export_leads_as_json(client):
    admin = await _signup_verify_login(client)
    lead_id = await _seed_lead(admin)
    r = await client.get("/api/v1/dashboard/leads/export?format=json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert "leads.json" in r.headers["content-disposition"]
    (row,) = r.json()
    assert row == {
        "leadId": str(lead_id),
        "name": "Jane",
        "contactIdentifier": "jane@x.com",
        "email": None,  # volunteered-email column (round 5A item 5) — NULL here
        "intent": "quote",
        "state": "active",
        "outcome": "in_progress",
        "lastActivityAt": row["lastActivityAt"],
    }


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_export_rejects_an_unknown_format(client):
    await _signup_verify_login(client)
    r = await client.get("/api/v1/dashboard/leads/export?format=xlsx")
    assert r.status_code == 422  # open formats only


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_export_leads_requires_a_session(client):
    r = await client.get("/api/v1/dashboard/leads/export")
    assert r.status_code == 401


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_export_leads_carries_only_the_caller_s_tenant(client):
    """The export is a bulk read of Customer Content, so it is the worst place for an RLS
    gap: one wrong row here is another business's lead list leaving the platform."""
    other = uuid.uuid4()
    async with tenant_session(other) as s:
        s.add(
            Lead(
                id=uuid.uuid4(),
                admin_id=other,
                instance_id=uuid.uuid4(),
                name="NotYours",
                contact_identifier="other@x.com",
                state=LeadState.active,
            )
        )
        await s.flush()

    admin = await _signup_verify_login(client)
    mine = await _seed_lead(admin)

    r = await client.get("/api/v1/dashboard/leads/export?format=json")
    assert [row["leadId"] for row in r.json()] == [str(mine)]
    assert "other@x.com" not in r.text


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
