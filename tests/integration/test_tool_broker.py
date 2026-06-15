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
