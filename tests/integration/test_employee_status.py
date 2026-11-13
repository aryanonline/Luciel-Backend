"""Round 6 WP-F: employee status is derived from rows that already exist, per tenant,
in the owner's local day. Pinned with a real database: the counts, the day
boundaries, tenant isolation, and that a capability is never ``ready`` without a
live connection.

Round 7 WP-3a: a "conversation" is a BILLED session — one the fire-once counter
(``app.runtime.budget.count_session_once``) wrote to the ``counted_sessions`` ledger.
The sessions here are counted through that real path, never by seeding the
deprecated ``sessions.counted`` column (which nothing in the product ever sets, so a
test that seeded it proved nothing about what the owner sees)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db.session import tenant_session
from app.employee.brief import render_daily_brief
from app.employee.status import build_employee_status
from app.models.audit import ActorType, AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.conversation import EscalationEvent
from app.models.conversation import Session as SessionModel
from app.models.instance import Instance, LucielState
from app.models.lead import Lead
from app.runtime.budget import count_session_once

# The clock is TODAY's date, not a fixed one. ``count_session_once`` counts into the
# billing period the WALL CLOCK is in, while the status read takes ``now`` — with a
# hard-coded September date the two agreed only during September 2026, and the
# nightly went red on 2026-10-01 with "0 conversations used" (round 7). Same UTC date
# as the wall clock = same billing period, on any day the suite runs.
_TODAY = datetime.now(UTC).date()
_YESTERDAY = _TODAY - timedelta(days=1)
# 15:00 UTC = 11:00 EDT / 10:00 EST in Toronto — mid-morning local either way.
NOW = datetime(_TODAY.year, _TODAY.month, _TODAY.day, 15, 0, tzinfo=UTC)
# 00:00 UTC today = 20:00 EDT / 19:00 EST YESTERDAY — a UTC "today" row that is a
# LOCAL "yesterday" row: the day boundary must be the owner's, not the server's.
LAST_NIGHT_LOCAL = datetime(_TODAY.year, _TODAY.month, _TODAY.day, 0, 0, tzinfo=UTC)
# 13:30 UTC = 09:30 EDT / 08:30 EST today.
THIS_MORNING = datetime(_TODAY.year, _TODAY.month, _TODAY.day, 13, 30, tzinfo=UTC)


async def _seed(s, admin, iid, *, channels, tools, connections=(), timezone="America/Toronto"):
    s.add(
        Instance(
            id=iid,
            admin_id=admin,
            name="Aurora",
            state=LucielState.active,
            channels=channels,
            tools=tools,
            escalation={"primaryEmail": "office@acme.example"},
            personality={"assistantName": "Aurora"},
            timezone=timezone,
            business_short_name="Acme",
        )
    )
    for connection_type, status in connections:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type=connection_type,
                auth_class="oauth",
                provider="outlook" if connection_type == "email_sender" else "twilio",
                status=status,
            )
        )
    await s.flush()


async def _session(s, admin, iid, *, at, channel="widget", counted=True) -> SessionModel:
    """A session row that opened at ``at``; ``counted`` runs it through the REAL
    fire-once counter (the ledger the Today card reads), exactly as the first model
    call does in production. ``counted=False`` is a session that opened and never
    reached a model call — not a conversation anywhere in the product."""
    row = SessionModel(
        id=uuid.uuid4(),
        admin_id=admin,
        instance_id=iid,
        channel=channel,
        mode="ai",
        started_at=at,
        last_message_at=at,
    )
    s.add(row)
    await s.flush()
    if counted:
        assert await count_session_once(s, admin_id=admin, instance_id=iid, session_id=row.id)
    return row


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_counts_follow_the_owners_local_day_and_only_counted_sessions():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        await _seed(s, admin, iid, channels=[{"id": "widget", "enabled": True}], tools=[])
        await _session(s, admin, iid, at=LAST_NIGHT_LOCAL)  # local yesterday
        await _session(s, admin, iid, at=LAST_NIGHT_LOCAL, channel="sms")
        await _session(s, admin, iid, at=THIS_MORNING)  # local today
        await _session(s, admin, iid, at=THIS_MORNING, counted=False)  # never a conversation
        s.add(
            Lead(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                name="Dana",
                contact_identifier="dana@lead.example",
                created_at=LAST_NIGHT_LOCAL,
                crm_push_status="synced",
                crm_pushed_at=LAST_NIGHT_LOCAL,
            )
        )
        s.add(
            EscalationEvent(
                escalation_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                signal="explicit_human_request",
                gate="intake",
                fired_at=LAST_NIGHT_LOCAL,
                delivery_status="delivered",
            )
        )
        s.add(
            AdminAuditLog(
                admin_id=admin,
                instance_id=iid,
                actor_type=ActorType.admin_user,
                event_type="human_takeover_started",
                event_payload={},
                created_at=THIS_MORNING,
            )
        )
        await s.flush()
        status = await build_employee_status(s, admin_id=admin, instance_id=iid, now=NOW)
    assert status is not None
    assert status.on_duty == "around the clock"
    assert status.business_short_name == "Acme"
    assert status.yesterday.date == _YESTERDAY.isoformat()
    assert status.yesterday.conversations == 2
    assert status.yesterday.by_channel == {"widget": 1, "sms": 1}
    assert status.yesterday.leads == 1 and status.yesterday.leads_to_crm == 1
    assert status.yesterday.escalations == 1 and status.yesterday.escalations_reached == 1
    assert status.today.date == _TODAY.isoformat()
    assert status.today.conversations == 1
    assert status.today.human_takeovers == 1
    assert status.today.leads == 0
    # The same ledger feeds the budget line, so the card and Billing agree (invariant:
    # a conversation means the same thing everywhere).
    assert status.conversations_used == 3
    # And the 7am brief reports the day instead of "A quiet day" (round 7 WP-3a).
    subject, body = render_daily_brief(status, local_date=_TODAY, due_callbacks_today=0)
    assert subject == "Yesterday at Acme: 2 conversations, 1 lead — Aurora's brief"
    # Equal counts tie, and the mix order then follows the GROUP BY — pin the facts,
    # not the tie-break.
    assert "- 2 conversations (" in body
    assert "1 on website chat" in body and "1 on text messages" in body
    assert "A quiet day" not in body


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_session_that_never_reached_a_model_call_is_not_a_conversation():
    """The live defect (round 7 WP-3a): sessions were counted by ``sessions.counted``,
    a column nothing sets, so a day with real billed conversations still read as
    quiet. The card reads the ``counted_sessions`` ledger — and ONLY that: an open
    session that never reached the fire-once counter is not a conversation, and a
    session counted twice (a retried turn) is still one."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        await _seed(s, admin, iid, channels=[{"id": "widget", "enabled": True}], tools=[])
        billed = await _session(s, admin, iid, at=THIS_MORNING, channel="whatsapp")
        await _session(s, admin, iid, at=THIS_MORNING, counted=False)
        # A second turn on the billed session hits the fire-once guard: no double count.
        assert not await count_session_once(
            s, admin_id=admin, instance_id=iid, session_id=billed.id
        )
        status = await build_employee_status(s, admin_id=admin, instance_id=iid, now=NOW)
    assert status is not None
    assert status.today.conversations == 1
    assert status.today.by_channel == {"whatsapp": 1}
    assert status.today.quiet is False
    assert status.yesterday.quiet is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_another_tenants_rows_never_count():
    a, ia = uuid.uuid4(), uuid.uuid4()
    b, ib = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(b) as s:
        await _seed(s, b, ib, channels=[{"id": "widget", "enabled": True}], tools=[])
        for _ in range(3):
            await _session(s, b, ib, at=THIS_MORNING)
    async with tenant_session(a) as s:
        await _seed(s, a, ia, channels=[{"id": "widget", "enabled": True}], tools=[])
        status = await build_employee_status(s, admin_id=a, instance_id=ia, now=NOW)
    assert status is not None
    assert status.today.conversations == 0 and status.yesterday.conversations == 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_capability_is_ready_only_with_a_live_connection():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        await _seed(
            s,
            admin,
            iid,
            channels=[
                {"id": "widget", "enabled": True},
                {"id": "email", "enabled": True},
                {"id": "sms", "enabled": True},
            ],
            tools=[
                {"id": "send_sms", "enabled": True},
                {"id": "schedule_callback", "enabled": True},
            ],
            connections=[("sms_sender", "connected")],
        )
        status = await build_employee_status(s, admin_id=admin, instance_id=iid, now=NOW)
    assert status is not None
    caps = {c.id: c.state for c in status.capabilities}
    assert caps["widget"] == "ready"
    assert caps["sms"] == "ready"
    assert caps["send_sms"] == "ready"
    assert caps["schedule_callback"] == "ready"
    assert caps["email"] == "attention"
    codes = [n.code for n in status.needs]
    assert "email_no_mailbox" in codes
    assert status.needs[0].severity == "attention"
    assert status.free_remaining == 50


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_no_timezone_means_utc_days_and_no_team_claim():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        await _seed(
            s, admin, iid, channels=[{"id": "widget", "enabled": True}], tools=[], timezone=None
        )
        await _session(s, admin, iid, at=LAST_NIGHT_LOCAL)  # 00:00 UTC today
        status = await build_employee_status(s, admin_id=admin, instance_id=iid, now=NOW)
    assert status is not None
    assert status.timezone is None
    assert status.today.conversations == 1 and status.yesterday.conversations == 0
    assert status.team_reachable_now is None
    assert "team_availability_unset" in [n.code for n in status.needs]
