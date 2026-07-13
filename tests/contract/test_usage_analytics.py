"""Contract conformance for /api/v1/admin/usage/* analytics + CSV export
(Architecture §3.9, Vision §7).

Covers the fields that were previously hardcoded/missing (appointments-booked,
response-time p50/p95, busiest-times, top-knowledge-sources,
conversion-by-channel) and asserts they are HONEST — never a fabricated 0/[]
where no store exists — plus the new CSV export endpoint (Vision §7:
"exportable reports — CSV download of any analytics view for the selected
period", available to ALL accounts).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.db.session import tenant_session
from app.models.conversation import EscalationEvent, Session
from app.models.instance import Instance, LucielState

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


async def _signup_verify_login(client) -> uuid.UUID:
    from app.auth import access

    email = f"an-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "t"},
    )
    acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    return acct.id


async def _seed_instance_with_activity(admin: uuid.UUID) -> uuid.UUID:
    iid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="L",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        s.add(
            Session(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                channel="widget",
                mode="ai",
                started_at=datetime.now(UTC),
                last_message_at=datetime.now(UTC),
            )
        )
        s.add(
            EscalationEvent(
                admin_id=admin,
                instance_id=iid,
                signal="cannot_answer",
                gate="intake",
                fired_at=datetime.now(UTC),
            )
        )
    return iid


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_usage_overview_new_fields_present_and_honest(client) -> None:
    admin = await _signup_verify_login(client)
    await _seed_instance_with_activity(admin)

    r = await client.get("/api/v1/admin/usage/overview")
    assert r.status_code == 200
    body = r.json()

    # Original frozen fields still present.
    for key in (
        "conversationsThisPeriod",
        "conversationsTotal",
        "leadsThisPeriod",
        "escalationsBySignal",
        "channelMix",
        "budgetUtilization",
        "busiestTimes",
    ):
        assert key in body, f"missing {key}"

    # New §3.9/§7 fields.
    for key in (
        "appointmentsBooked",
        "appointmentsBookedNote",
        "responseTimeP50Seconds",
        "responseTimeP95Seconds",
        "responseTimeNote",
        "topKnowledgeSources",
        "topKnowledgeSourcesNote",
        "conversionByChannel",
        "conversionBySourceNote",
        "conversionByServiceNote",
    ):
        assert key in body, f"missing {key}"

    # Honest gaps: null + a note, never a fabricated 0/[].
    assert body["appointmentsBooked"] is None
    assert body["appointmentsBookedNote"]
    assert body["responseTimeP50Seconds"] is None
    assert body["responseTimeP95Seconds"] is None
    assert body["responseTimeNote"]
    assert body["topKnowledgeSources"] == []
    assert body["topKnowledgeSourcesNote"]
    assert body["conversionBySourceNote"]
    assert body["conversionByServiceNote"]

    # Real data: busiest-times heatmap shape + at least one session counted, and
    # channel mix / conversion-by-channel reflect the seeded widget session.
    assert len(body["busiestTimes"]) == 7
    assert all(len(row) == 24 for row in body["busiestTimes"])
    assert sum(sum(row) for row in body["busiestTimes"]) >= 1
    assert any(c["channel"] == "widget" for c in body["channelMix"])
    assert any(c["channel"] == "widget" for c in body["conversionByChannel"])
    assert any(e["signal"] == "cannot_answer" for e in body["escalationsBySignal"])


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_usage_export_csv_overview(client) -> None:
    admin = await _signup_verify_login(client)
    await _seed_instance_with_activity(admin)

    r = await client.get("/api/v1/admin/usage/export.csv", params={"view": "overview"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    text = r.text
    assert "metric,value" in text
    assert "conversations_this_period" in text
    assert "not_available" in text  # appointments/response-time honest gap


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_usage_export_csv_busiest_times_and_channel_mix(client) -> None:
    admin = await _signup_verify_login(client)
    await _seed_instance_with_activity(admin)

    r_bt = await client.get("/api/v1/admin/usage/export.csv", params={"view": "busiest_times"})
    assert r_bt.status_code == 200
    assert "day_of_week" in r_bt.text
    assert "hour_0" in r_bt.text and "hour_23" in r_bt.text

    r_cm = await client.get("/api/v1/admin/usage/export.csv", params={"view": "channel_mix"})
    assert r_cm.status_code == 200
    assert "widget" in r_cm.text

    r_cc = await client.get("/api/v1/admin/usage/export.csv", params={"view": "conversion_by_channel"})
    assert r_cc.status_code == 200
    assert "conversion_rate" in r_cc.text


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_usage_export_csv_unknown_view_404(client) -> None:
    admin = await _signup_verify_login(client)
    await _seed_instance_with_activity(admin)

    r = await client.get("/api/v1/admin/usage/export.csv", params={"view": "not_a_view"})
    assert r.status_code == 404


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_usage_export_csv_requires_auth(client) -> None:
    r = await client.get("/api/v1/admin/usage/export.csv", params={"view": "overview"})
    assert r.status_code in (401, 403)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_usage_export_csv_audit_log_view(client) -> None:
    admin = await _signup_verify_login(client)
    await _seed_instance_with_activity(admin)

    r = await client.get("/api/v1/admin/usage/export.csv", params={"view": "audit_log"})
    assert r.status_code == 200
    assert "event_id,event_type,at,actor_user_id" in r.text
