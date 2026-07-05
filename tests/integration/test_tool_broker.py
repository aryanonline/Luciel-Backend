"""Tool broker two-gate dispatch + five-outcome contract (Architecture §3.3.3/§3.3.4)."""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.models.connection import InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.tool_auth import InstanceToolAuthorization
from app.tools import catalog
from app.tools.broker import ToolBroker
from app.tools.contract import Outcome

pytestmark = [pytest.mark.asyncio]


# --- fake backends exercising each outcome --------------------------------- #
class FakeCalendar:
    def __init__(self, slots=None, book_result="bookable"):
        self._slots = slots if slots is not None else ["2026-06-20T14:00Z"]
        self._book = book_result

    async def free_busy(self, ctx, window):
        return self._slots

    async def book(self, ctx, slot, email):
        if self._book == "conflict":
            return None
        if self._book == "raise":
            raise RuntimeError("provider down")
        return {"bookingId": "bk1", "confirmedTime": slot}


class FakeRecords:
    def __init__(self, matches):
        self._m = matches

    async def lookup(self, ctx, query):
        return self._m


async def _seed(admin, *, authorize, connect_calendar="connected"):
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin}@x.com",
                email_lower=f"{admin}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        iid = uuid.uuid4()
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
        for tool_id in authorize:
            s.add(
                InstanceToolAuthorization(
                    id=uuid.uuid4(), admin_id=admin, instance_id=iid, tool_id=tool_id, enabled=True
                )
            )
        if connect_calendar:
            s.add(
                InstanceConnection(
                    connection_id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    connection_type="calendar",
                    auth_class="oauth_token",
                    provider="google_calendar",
                    status=connect_calendar,
                )
            )
        await s.flush()
        return iid


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_default_deny_unauthorized_tool():
    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=[])  # nothing authorized
    async with tenant_session(admin) as s:
        broker = ToolBroker(s)
        tool = catalog.CheckAvailabilityTool(FakeCalendar())
        res = await broker.dispatch(tool, {"window": "next week"}, admin_id=admin, instance_id=iid)
        assert res.outcome is Outcome.failed
        assert res.reason == "not_authorized"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_connection_gate_blocks_when_not_connected():
    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=["check_availability"], connect_calendar="expired")
    async with tenant_session(admin) as s:
        broker = ToolBroker(s)
        res = await broker.dispatch(
            catalog.CheckAvailabilityTool(FakeCalendar()),
            {"window": "x"},
            admin_id=admin,
            instance_id=iid,
        )
        assert res.outcome is Outcome.failed
        assert res.reason.startswith("connection_not_ready:calendar")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_missing_input_before_execution():
    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=["book_appointment"])
    async with tenant_session(admin) as s:
        broker = ToolBroker(s)
        # book_appointment requires slot + email; omit email.
        res = await broker.dispatch(
            catalog.BookAppointmentTool(FakeCalendar()),
            {"slot": "2026-06-20T14:00Z"},
            admin_id=admin,
            instance_id=iid,
        )
        assert res.outcome is Outcome.missing_input
        assert res.missing_fields == ["email"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_check_availability_ok_and_empty():
    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=["check_availability"])
    async with tenant_session(admin) as s:
        broker = ToolBroker(s)
        ok = await broker.dispatch(
            catalog.CheckAvailabilityTool(FakeCalendar(slots=["s1"])),
            {"window": "x"},
            admin_id=admin,
            instance_id=iid,
        )
        assert ok.outcome is Outcome.ok and ok.payload["slots"] == ["s1"]
        empty = await broker.dispatch(
            catalog.CheckAvailabilityTool(FakeCalendar(slots=[])),
            {"window": "x"},
            admin_id=admin,
            instance_id=iid,
        )
        assert empty.outcome is Outcome.empty


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_book_conflict_is_failed_never_success():
    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=["book_appointment"])
    async with tenant_session(admin) as s:
        broker = ToolBroker(s)
        res = await broker.dispatch(
            catalog.BookAppointmentTool(FakeCalendar(book_result="conflict")),
            {"slot": "s1", "email": "j@x.com"},
            admin_id=admin,
            instance_id=iid,
        )
        assert res.outcome is Outcome.failed and res.reason == "slot_conflict"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_lookup_record_ok_empty_ambiguous():
    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=["lookup_record"], connect_calendar=None)
    async with tenant_session(admin) as s:
        # connect a record_source
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="record_source",
                auth_class="api_key",
                provider="csv",
                status="connected",
            )
        )
        await s.flush()
        broker = ToolBroker(s)
        one = await broker.dispatch(
            catalog.LookupRecordTool(FakeRecords([{"id": 1}])),
            {"query": "q"},
            admin_id=admin,
            instance_id=iid,
        )
        assert one.outcome is Outcome.ok
        none = await broker.dispatch(
            catalog.LookupRecordTool(FakeRecords([])),
            {"query": "q"},
            admin_id=admin,
            instance_id=iid,
        )
        assert none.outcome is Outcome.empty
        many = await broker.dispatch(
            catalog.LookupRecordTool(FakeRecords([{"id": 1}, {"id": 2}])),
            {"query": "q"},
            admin_id=admin,
            instance_id=iid,
        )
        assert many.outcome is Outcome.ambiguous
        assert len(many.payload["candidates"]) == 2


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_provider_exception_normalized_to_failed_not_500():
    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=["book_appointment"])
    async with tenant_session(admin) as s:
        broker = ToolBroker(s)
        res = await broker.dispatch(
            catalog.BookAppointmentTool(FakeCalendar(book_result="raise")),
            {"slot": "s1", "email": "j@x.com"},
            admin_id=admin,
            instance_id=iid,
        )
        assert res.outcome is Outcome.failed  # never raises out of the broker


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_schedule_callback_no_connection_gate():
    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=["schedule_callback"], connect_calendar=None)
    async with tenant_session(admin) as s:
        broker = ToolBroker(s)
        res = await broker.dispatch(
            catalog.ScheduleCallbackTool(),
            {"fire_at": "2026-06-20T09:00Z", "channel": "email", "message": "follow up"},
            admin_id=admin,
            instance_id=iid,
        )
        assert res.outcome is Outcome.ok  # passes gate 2 (requires_connection None)


# ---------------------------------------------------------------------------
# Regression tests for Axis5-F01 (Gate 2 channel prerequisite) and
# Axis5-F03 (ScheduleCallbackTool durable write)
# ---------------------------------------------------------------------------


async def _seed_with_email_channel(admin, *, authorize, email_channel_enabled=True):
    """Seed with an email channel that is optionally enabled."""
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin}@x.com",
                email_lower=f"{admin}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        iid = uuid.uuid4()
        channels = [{"id": "email", "enabled": email_channel_enabled}]
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="L",
                state=LucielState.active,
                channels=channels,
                tools=[],
                escalation={},
                personality={},
            )
        )
        for tool_id in authorize:
            s.add(
                InstanceToolAuthorization(
                    id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    tool_id=tool_id,
                    enabled=True,
                )
            )
        # Always add a connected email_sender connection
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="email_sender",
                auth_class="api_key",
                provider="ses",
                status="connected",
            )
        )
        await s.flush()
        return iid


class FakeMessenger:
    async def send(self, ctx, to, body):
        return "msg-id-1"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_gate2_channel_off_blocks_send_email():
    """Axis5-F01: Gate 2 blocks send_email when email channel is disabled."""
    admin = uuid.uuid4()
    iid = await _seed_with_email_channel(
        admin, authorize=["send_email"], email_channel_enabled=False
    )
    async with tenant_session(admin) as s:
        broker = ToolBroker(s)
        res = await broker.dispatch(
            catalog.SendEmailTool(FakeMessenger()),
            {"to": "a@b.com", "body": "hello"},
            admin_id=admin,
            instance_id=iid,
        )
    assert res.outcome is Outcome.failed
    assert "channel_not_enabled:email" in (res.reason or "")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_gate2_channel_on_allows_send_email():
    """Axis5-F01: Gate 2 passes when email channel is enabled."""
    admin = uuid.uuid4()
    iid = await _seed_with_email_channel(
        admin, authorize=["send_email"], email_channel_enabled=True
    )
    async with tenant_session(admin) as s:
        broker = ToolBroker(s)
        res = await broker.dispatch(
            catalog.SendEmailTool(FakeMessenger()),
            {"to": "a@b.com", "body": "hello"},
            admin_id=admin,
            instance_id=iid,
        )
    assert res.outcome is Outcome.ok


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_schedule_callback_writes_deferred_action_row():
    """Axis5-F03: ScheduleCallbackTool.execute() must persist a DeferredAction row."""
    from app.models.deferred import DeferredAction
    from sqlalchemy import func
    from sqlalchemy import select as sa_select

    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=["schedule_callback"], connect_calendar=None)
    async with tenant_session(admin) as s:
        broker = ToolBroker(s)
        res = await broker.dispatch(
            catalog.ScheduleCallbackTool(),
            {"fire_at": "2026-06-25T10:00Z", "channel": "email", "message": "follow up"},
            admin_id=admin,
            instance_id=iid,
        )
        assert res.outcome is Outcome.ok, f"Expected ok, got {res.outcome}: {res.reason}"
        count = (
            await s.execute(
                sa_select(func.count())
                .select_from(DeferredAction)
                .where(DeferredAction.admin_id == admin)
            )
        ).scalar_one()
        assert count == 1, f"Expected 1 DeferredAction row, got {count}"


# ---------------------------------------------------------------------------
# P0-3 (§3.3.5 / Decision #51): schedule_callback rate-limit — enforced at
# scheduling time, expressed as a structured `failed` reason, no row on reject.
# ---------------------------------------------------------------------------


async def _count_queued_callbacks(s, admin, *, resolved_lead_id=None) -> int:
    from app.models.deferred import DeferredAction
    from sqlalchemy import func
    from sqlalchemy import select as sa_select

    stmt = (
        sa_select(func.count())
        .select_from(DeferredAction)
        .where(
            DeferredAction.admin_id == admin,
            DeferredAction.action_type == "schedule_callback",
            DeferredAction.status == "queued",
        )
    )
    if resolved_lead_id is not None:
        stmt = stmt.where(DeferredAction.resolved_lead_id == resolved_lead_id)
    return (await s.execute(stmt)).scalar_one()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_schedule_callback_per_lead_cap_and_no_row_on_reject():
    """§3.3.5: at the per-lead pending cap (3), the 3rd callback for a lead succeeds
    and the 4th returns a structured rate_limited:per_lead — with NO row written and
    the rejection traced."""
    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=["schedule_callback"], connect_calendar=None)
    lead = uuid.uuid4()
    cap = catalog.SCHEDULE_CALLBACK_PER_LEAD_PENDING_CAP
    async with tenant_session(admin) as s:
        broker = ToolBroker(s)
        tool = catalog.ScheduleCallbackTool()
        # First `cap` (3) callbacks for this lead succeed.
        for i in range(cap):
            res = await broker.dispatch(
                tool,
                {"fire_at": f"2026-06-25T1{i}:00Z", "channel": "email", "message": "follow up"},
                admin_id=admin,
                instance_id=iid,
                resolved_lead_id=lead,
            )
            assert res.outcome is Outcome.ok, f"call {i}: {res.reason}"

        # The (cap+1)-th is rate-limited: structured failed reason, not a new Outcome.
        res = await broker.dispatch(
            tool,
            {"fire_at": "2026-06-25T20:00Z", "channel": "email", "message": "follow up"},
            admin_id=admin,
            instance_id=iid,
            resolved_lead_id=lead,
        )
        assert res.outcome is Outcome.failed
        assert res.reason == "rate_limited:per_lead"

        # No row written on rejection — exactly `cap` queued for the lead.
        assert await _count_queued_callbacks(s, admin, resolved_lead_id=lead) == cap
        # The rejection was traced (§3.3.4 rule 3).
        last = broker.traces[-1]
        assert last["outcome"] == "failed" and last["reason"] == "rate_limited:per_lead"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_schedule_callback_per_lead_cap_isolated_per_lead():
    """The per-lead cap is scoped per lead: a second lead is unaffected by the first
    lead being at cap."""
    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=["schedule_callback"], connect_calendar=None)
    lead_a, lead_b = uuid.uuid4(), uuid.uuid4()
    cap = catalog.SCHEDULE_CALLBACK_PER_LEAD_PENDING_CAP
    async with tenant_session(admin) as s:
        broker = ToolBroker(s)
        tool = catalog.ScheduleCallbackTool()
        for i in range(cap):
            await broker.dispatch(
                tool,
                {"fire_at": f"2026-06-25T1{i}:00Z", "channel": "email", "message": "m"},
                admin_id=admin,
                instance_id=iid,
                resolved_lead_id=lead_a,
            )
        # lead_a is at cap; lead_b's first callback still succeeds.
        res = await broker.dispatch(
            tool,
            {"fire_at": "2026-06-25T21:00Z", "channel": "email", "message": "m"},
            admin_id=admin,
            instance_id=iid,
            resolved_lead_id=lead_b,
        )
        assert res.outcome is Outcome.ok


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_schedule_callback_per_tenant_cap():
    """§3.3.5: over the per-tenant pending ceiling, schedule_callback returns a
    structured rate_limited:per_tenant even when no lead key is present (anonymous
    session) — the per-tenant axis is enforced unconditionally."""
    from app.connections import deferred_queue

    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=["schedule_callback"], connect_calendar=None)
    cap = catalog.SCHEDULE_CALLBACK_PER_TENANT_PENDING_CAP
    async with tenant_session(admin) as s:
        # Seed the tenant right up to its pending ceiling (distinct anonymous rows).
        for _ in range(cap):
            await deferred_queue.enqueue(
                s,
                admin_id=admin,
                instance_id=iid,
                action_type="schedule_callback",
                connection_type="email_sender",
                payload={"fire_at": "2026-06-25T10:00Z", "channel": "email", "message": "m"},
            )
        broker = ToolBroker(s)
        # No resolved_lead_id (anonymous) → per-lead axis skipped, per-tenant fires.
        res = await broker.dispatch(
            catalog.ScheduleCallbackTool(),
            {"fire_at": "2026-06-25T22:00Z", "channel": "email", "message": "m"},
            admin_id=admin,
            instance_id=iid,
        )
        assert res.outcome is Outcome.failed
        assert res.reason == "rate_limited:per_tenant"
        # No row beyond the seeded ceiling.
        assert await _count_queued_callbacks(s, admin) == cap
