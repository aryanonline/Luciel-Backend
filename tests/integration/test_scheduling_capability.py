"""Scheduling as ONE capability over the single calendar connection (Decision #8).

Before this, Luciel could offer and book a time but had no way to MOVE or CANCEL one — an
owner's customer who needed to change an appointment had to reach a human, which is exactly
the "demo, not something you can own" gap the product review called out.

Two things are asserted here:
  * ``reschedule_appointment`` / ``cancel_appointment`` are real dispatchable tools that
    honour the five-outcome contract (§3.3.4) and are READ-BEFORE-WRITE (§3.3.6) — they
    never write against a booking they have not first confirmed exists and is unambiguous.
  * All four scheduling tools ride the SAME ``calendar`` connection (§3.8.2 single active
    per type), so they connect once and gate together — no second calendar connection and
    no per-tool credential.
"""

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

_BOOKING = {"bookingRef": "bk-1", "start": "2026-08-01T14:00Z", "email": "lead@x.com"}


class FakeCalendar:
    """Records what it was asked, so a test can prove the READ happened before the WRITE."""

    def __init__(self, *, found=(_BOOKING,), write="ok"):
        self._found = list(found)
        self._write = write
        self.calls: list[str] = []

    async def free_busy(self, ctx, window):
        self.calls.append("free_busy")
        return ["2026-08-01T15:00Z"]

    async def book(self, ctx, slot, email):
        self.calls.append("book")
        return {"bookingRef": "bk-new", "confirmedTime": slot}

    async def find_bookings(self, ctx, *, email, booking_ref=None):
        self.calls.append("find_bookings")
        if booking_ref is not None:
            return [b for b in self._found if b.get("bookingRef") == booking_ref]
        return list(self._found)

    async def reschedule(self, ctx, booking_ref, slot):
        self.calls.append(f"reschedule:{booking_ref}")
        if self._write == "conflict":
            return None
        if self._write == "raise":
            raise RuntimeError("provider down")
        return {"bookingRef": booking_ref, "confirmedTime": slot}

    async def cancel(self, ctx, booking_ref):
        self.calls.append(f"cancel:{booking_ref}")
        if self._write == "conflict":
            return None
        if self._write == "raise":
            raise RuntimeError("provider down")
        return {"bookingRef": booking_ref, "cancelled": True}


async def _seed(admin, *, authorize, calendar_status="connected"):
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
        if calendar_status:
            s.add(
                InstanceConnection(
                    connection_id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    connection_type="calendar",
                    auth_class="oauth_token",
                    provider="google_calendar",
                    status=calendar_status,
                )
            )
        await s.flush()
        return iid


# --- the capability is ONE connection --------------------------------------- #


async def test_all_four_scheduling_tools_share_the_single_calendar_connection():
    assert catalog.SCHEDULING_TOOL_IDS == (
        "check_availability",
        "book_appointment",
        "reschedule_appointment",
        "cancel_appointment",
    )
    for tool_id in catalog.SCHEDULING_TOOL_IDS:
        assert catalog.TOOL_CONNECTION_TYPE[tool_id] == "calendar", tool_id
        assert catalog.capability_of(tool_id) == "scheduling", tool_id
    # And they are registered as real, buildable tools — not catalog entries only.
    from app.tools.registry import build_tool

    for tool_id in catalog.SCHEDULING_TOOL_IDS:
        tool = build_tool(tool_id)
        assert tool is not None, tool_id
        assert tool.requires_connection == "calendar", tool_id
        assert tool.tool_id == tool_id


async def test_the_new_tools_are_in_the_catalog_and_have_required_inputs():
    assert "reschedule_appointment" in catalog.ALL_TOOL_IDS
    assert "cancel_appointment" in catalog.ALL_TOOL_IDS
    assert catalog.TOOL_REQUIRED_INPUTS["reschedule_appointment"] == frozenset({"email", "slot"})
    assert catalog.TOOL_REQUIRED_INPUTS["cancel_appointment"] == frozenset({"email"})


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_one_calendar_connection_serves_reschedule_and_cancel():
    """A single connected calendar row is enough for the whole capability — the two new
    tools do NOT need a connection of their own (§3.8.2)."""
    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=["reschedule_appointment", "cancel_appointment"])
    backend = FakeCalendar()
    async with tenant_session(admin) as s:
        broker = ToolBroker(s)
        moved = await broker.dispatch(
            catalog.RescheduleAppointmentTool(backend),
            {"email": "lead@x.com", "slot": "2026-08-02T09:00Z"},
            admin_id=admin,
            instance_id=iid,
        )
        cancelled = await broker.dispatch(
            catalog.CancelAppointmentTool(backend),
            {"email": "lead@x.com"},
            admin_id=admin,
            instance_id=iid,
        )
        rows = list(
            (
                await s.execute(
                    InstanceConnection.__table__.select().where(
                        InstanceConnection.instance_id == iid
                    )
                )
            ).all()
        )
    assert moved.outcome is Outcome.ok
    assert cancelled.outcome is Outcome.ok
    assert len(rows) == 1, "one calendar connection, not one per tool"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_disconnected_calendar_blocks_reschedule_and_cancel_together():
    """Gate 2 (§3.8.7 rule A) keys on the shared connection, so handing the calendar back
    (Decision #5b) takes the whole capability with it rather than half of it."""
    admin = uuid.uuid4()
    iid = await _seed(
        admin,
        authorize=["reschedule_appointment", "cancel_appointment"],
        calendar_status="not_connected",
    )
    async with tenant_session(admin) as s:
        broker = ToolBroker(s)
        for tool, inputs in (
            (catalog.RescheduleAppointmentTool(FakeCalendar()), {"email": "a@x.com", "slot": "s"}),
            (catalog.CancelAppointmentTool(FakeCalendar()), {"email": "a@x.com"}),
        ):
            res = await broker.dispatch(tool, inputs, admin_id=admin, instance_id=iid)
            assert res.outcome is Outcome.failed
            assert res.reason.startswith("connection_not_ready:calendar")


# --- five-outcome contract, read-before-write ------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_reschedule_reads_the_booking_before_it_writes():
    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=["reschedule_appointment"])
    backend = FakeCalendar()
    async with tenant_session(admin) as s:
        res = await ToolBroker(s).dispatch(
            catalog.RescheduleAppointmentTool(backend),
            {"email": "lead@x.com", "slot": "2026-08-02T09:00Z"},
            admin_id=admin,
            instance_id=iid,
        )
    assert res.outcome is Outcome.ok
    assert res.payload["confirmedTime"] == "2026-08-02T09:00Z"
    # The READ strictly precedes the WRITE, and the write targets the ref the read returned.
    assert backend.calls == ["find_bookings", "reschedule:bk-1"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_cancel_reads_the_booking_before_it_writes():
    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=["cancel_appointment"])
    backend = FakeCalendar()
    async with tenant_session(admin) as s:
        res = await ToolBroker(s).dispatch(
            catalog.CancelAppointmentTool(backend),
            {"email": "lead@x.com"},
            admin_id=admin,
            instance_id=iid,
        )
    assert res.outcome is Outcome.ok
    assert res.payload["cancelled"] is True
    assert backend.calls == ["find_bookings", "cancel:bk-1"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_no_matching_booking_is_empty_not_failed():
    """Nothing to move is a legitimate EMPTY read, not an error — the loop should say
    "I don't see a booking for that address", not "something went wrong" (§3.3.4)."""
    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=["reschedule_appointment", "cancel_appointment"])
    backend = FakeCalendar(found=())
    async with tenant_session(admin) as s:
        broker = ToolBroker(s)
        moved = await broker.dispatch(
            catalog.RescheduleAppointmentTool(backend),
            {"email": "nobody@x.com", "slot": "s"},
            admin_id=admin,
            instance_id=iid,
        )
        cancelled = await broker.dispatch(
            catalog.CancelAppointmentTool(backend),
            {"email": "nobody@x.com"},
            admin_id=admin,
            instance_id=iid,
        )
    assert moved.outcome is Outcome.empty
    assert cancelled.outcome is Outcome.empty
    # Crucially: no write was attempted against a booking we never found.
    assert backend.calls == ["find_bookings", "find_bookings"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_multiple_bookings_are_ambiguous_and_nothing_is_written():
    """Cancelling the WRONG appointment is worse than asking which one (§3.3.6)."""
    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=["cancel_appointment"])
    second = {"bookingRef": "bk-2", "start": "2026-08-05T10:00Z", "email": "lead@x.com"}
    backend = FakeCalendar(found=(_BOOKING, second))
    async with tenant_session(admin) as s:
        res = await ToolBroker(s).dispatch(
            catalog.CancelAppointmentTool(backend),
            {"email": "lead@x.com"},
            admin_id=admin,
            instance_id=iid,
        )
    assert res.outcome is Outcome.ambiguous
    assert {c["bookingRef"] for c in res.payload["candidates"]} == {"bk-1", "bk-2"}
    assert backend.calls == ["find_bookings"], "no write on an ambiguous read"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_explicit_booking_ref_resolves_an_otherwise_ambiguous_read():
    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=["cancel_appointment"])
    second = {"bookingRef": "bk-2", "start": "2026-08-05T10:00Z", "email": "lead@x.com"}
    backend = FakeCalendar(found=(_BOOKING, second))
    async with tenant_session(admin) as s:
        res = await ToolBroker(s).dispatch(
            catalog.CancelAppointmentTool(backend),
            {"email": "lead@x.com", "booking_ref": "bk-2"},
            admin_id=admin,
            instance_id=iid,
        )
    assert res.outcome is Outcome.ok
    assert backend.calls == ["find_bookings", "cancel:bk-2"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_missing_required_input_is_missing_input():
    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=["reschedule_appointment"])
    async with tenant_session(admin) as s:
        res = await ToolBroker(s).dispatch(
            catalog.RescheduleAppointmentTool(FakeCalendar()),
            {"email": "lead@x.com"},  # no slot
            admin_id=admin,
            instance_id=iid,
        )
    assert res.outcome is Outcome.missing_input
    assert res.missing_fields == ["slot"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_slot_taken_between_read_and_write_is_failed_never_ok():
    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=["reschedule_appointment"])
    async with tenant_session(admin) as s:
        res = await ToolBroker(s).dispatch(
            catalog.RescheduleAppointmentTool(FakeCalendar(write="conflict")),
            {"email": "lead@x.com", "slot": "2026-08-02T09:00Z"},
            admin_id=admin,
            instance_id=iid,
        )
    assert res.outcome is Outcome.failed
    assert res.reason == "slot_conflict"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_provider_error_is_failed_with_a_non_secret_reason():
    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=["cancel_appointment"])
    async with tenant_session(admin) as s:
        res = await ToolBroker(s).dispatch(
            catalog.CancelAppointmentTool(FakeCalendar(write="raise")),
            {"email": "lead@x.com"},
            admin_id=admin,
            instance_id=iid,
        )
    assert res.outcome is Outcome.failed
    assert res.reason == "calendar_error: RuntimeError"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_default_deny_still_applies_to_the_new_tools():
    """A new tool is not a new hole: gate 1 is default-deny (§3.3.3)."""
    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=[])
    async with tenant_session(admin) as s:
        broker = ToolBroker(s)
        for tool, inputs in (
            (catalog.RescheduleAppointmentTool(FakeCalendar()), {"email": "a@x.com", "slot": "s"}),
            (catalog.CancelAppointmentTool(FakeCalendar()), {"email": "a@x.com"}),
        ):
            res = await broker.dispatch(tool, inputs, admin_id=admin, instance_id=iid)
            assert res.outcome is Outcome.failed
            assert res.reason == "not_authorized"
