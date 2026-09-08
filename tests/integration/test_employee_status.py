"""Round 6 WP-F: employee status is derived from rows that already exist, per tenant,
in the owner's local day. Pinned with a real database: the counts, the day
boundaries, tenant isolation, and that a capability is never ``ready`` without a
live connection."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.db.session import tenant_session
from app.employee.status import build_employee_status
from app.models.audit import ActorType, AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.conversation import EscalationEvent
from app.models.conversation import Session as SessionModel
from app.models.instance import Instance, LucielState
from app.models.lead import Lead

pytestmark = [pytest.mark.asyncio]

# Tuesday 2026-09-08 15:00 UTC = 11:00 EDT in Toronto.
NOW = datetime(2026, 9, 8, 15, 0, tzinfo=UTC)
# Yesterday (Monday) 20:00 EDT = Tuesday 00:00 UTC — a UTC "today" row that is a
# LOCAL "yesterday" row: the day boundary must be the owner's, not the server's.
LAST_NIGHT_LOCAL = datetime(2026, 9, 8, 0, 0, tzinfo=UTC)
THIS_MORNING = datetime(2026, 9, 8, 13, 30, tzinfo=UTC)  # 09:30 EDT today


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


def _session(admin, iid, *, at, channel="widget", counted=True):
    return SessionModel(
        id=uuid.uuid4(),
        admin_id=admin,
        instance_id=iid,
        channel=channel,
        mode="ai",
        counted=counted,
        started_at=at,
        last_message_at=at,
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_counts_follow_the_owners_local_day_and_only_counted_sessions():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        await _seed(s, admin, iid, channels=[{"id": "widget", "enabled": True}], tools=[])
        s.add(_session(admin, iid, at=LAST_NIGHT_LOCAL))  # local yesterday
        s.add(_session(admin, iid, at=LAST_NIGHT_LOCAL, channel="sms"))
        s.add(_session(admin, iid, at=THIS_MORNING))  # local today
        s.add(_session(admin, iid, at=THIS_MORNING, counted=False))  # never a conversation
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
    assert status.yesterday.date == "2026-09-07"
    assert status.yesterday.conversations == 2
    assert status.yesterday.by_channel == {"widget": 1, "sms": 1}
    assert status.yesterday.leads == 1 and status.yesterday.leads_to_crm == 1
    assert status.yesterday.escalations == 1 and status.yesterday.escalations_reached == 1
    assert status.today.date == "2026-09-08"
    assert status.today.conversations == 1
    assert status.today.human_takeovers == 1
    assert status.today.leads == 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_another_tenants_rows_never_count():
    a, ia = uuid.uuid4(), uuid.uuid4()
    b, ib = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(b) as s:
        await _seed(s, b, ib, channels=[{"id": "widget", "enabled": True}], tools=[])
        for _ in range(3):
            s.add(_session(b, ib, at=THIS_MORNING))
        await s.flush()
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
        s.add(_session(admin, iid, at=LAST_NIGHT_LOCAL))  # 00:00 UTC today
        await s.flush()
        status = await build_employee_status(s, admin_id=admin, instance_id=iid, now=NOW)
    assert status is not None
    assert status.timezone is None
    assert status.today.conversations == 1 and status.yesterday.conversations == 0
    assert status.team_reachable_now is None
    assert "team_availability_unset" in [n.code for n in status.needs]
