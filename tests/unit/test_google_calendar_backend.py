"""Google Calendar scheduling, end to end through the tools (§3.3.6).

Hermetic: the live/mock seam (``build_calendar_api``) yields the in-memory transport in
mock mode, which is the default, so nothing here can reach googleapis.com. What is under
test is the PRODUCTION code path — the token lookup, the read-before-write conflict
discipline, and the five-outcome mapping are the same objects in live mode; only the
transport differs.

Exercised through the catalog tools rather than the backend directly, because the thing
that matters to a lead is the OUTCOME: a taken slot must be ``failed``, never ``ok`` and
never ``empty``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.connections.secrets import LocalSecretStore, get_secret_store, secret_ref_for
from app.tools import catalog
from app.tools.calendar_backend import (
    GoogleCalendarBackend,
    InMemoryCalendarApi,
    open_slots,
    parse_interval,
)
from app.tools.contract import Outcome, ToolContext

LEAD = "lead@example.com"


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    LocalSecretStore.reset()
    InMemoryCalendarApi.reset()


def _window_start() -> datetime:
    """A whole hour comfortably in the future, so a 180-day booking search finds it."""
    return (datetime.now(UTC) + timedelta(days=2)).replace(
        minute=0, second=0, microsecond=0, hour=9
    )


async def _connected_context() -> ToolContext:
    """A tenant whose Google Calendar connect actually completed (token on file)."""
    admin, instance = uuid.uuid4(), uuid.uuid4()
    await get_secret_store().put(
        secret_ref_for(admin, instance, "calendar"),
        {"access_token": f"tok-{admin}"},
    )
    return ToolContext(admin_id=admin, instance_id=instance, connection_status="connected")


def _backend() -> GoogleCalendarBackend:
    return GoogleCalendarBackend()


def _slot(start: datetime, minutes: int = 30) -> str:
    return f"{start.isoformat()}/{(start + timedelta(minutes=minutes)).isoformat()}"


# --- window/slot maths ------------------------------------------------------ #
def test_parse_interval_accepts_range_instant_and_bare_date() -> None:
    start, end = parse_interval("2026-08-03T09:00:00+00:00/2026-08-03T11:00:00+00:00")
    assert (end - start) == timedelta(hours=2)
    start, end = parse_interval("2026-08-03T09:00:00+00:00")
    assert (end - start) == timedelta(minutes=30)
    start, end = parse_interval("2026-08-03")
    assert (end - start) == timedelta(days=1)


def test_parse_interval_rejects_a_backwards_window() -> None:
    with pytest.raises(ValueError, match="ends before it starts"):
        parse_interval("2026-08-03T11:00:00+00:00/2026-08-03T09:00:00+00:00")


def test_open_slots_excludes_overlaps_but_allows_back_to_back() -> None:
    start = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
    busy = [(start, start + timedelta(minutes=30))]
    slots = open_slots(busy, window_start=start, window_end=start + timedelta(hours=2), minutes=30)
    # 09:00 is taken; 09:30 abuts the busy block and is still bookable (half-open).
    assert slots == [
        (start + timedelta(minutes=30)).isoformat(),
        (start + timedelta(minutes=60)).isoformat(),
        (start + timedelta(minutes=90)).isoformat(),
    ]


# --- availability ----------------------------------------------------------- #
async def test_check_availability_reads_free_busy_and_omits_taken_slots() -> None:
    ctx = await _connected_context()
    start = _window_start()
    window = f"{start.isoformat()}/{(start + timedelta(hours=2)).isoformat()}"

    booked = await catalog.BookAppointmentTool(_backend()).execute(
        {"slot": _slot(start), "email": LEAD}, ctx
    )
    assert booked.outcome is Outcome.ok

    result = await catalog.CheckAvailabilityTool(_backend()).execute({"window": window}, ctx)
    assert result.outcome is Outcome.ok
    assert start.isoformat() not in result.payload["slots"]
    assert (start + timedelta(minutes=30)).isoformat() in result.payload["slots"]


async def test_check_availability_on_a_full_window_is_empty_not_failed() -> None:
    ctx = await _connected_context()
    start = _window_start()
    await catalog.BookAppointmentTool(_backend()).execute(
        {"slot": _slot(start, minutes=60), "email": LEAD}, ctx
    )
    result = await catalog.CheckAvailabilityTool(_backend()).execute(
        {"window": f"{start.isoformat()}/{(start + timedelta(hours=1)).isoformat()}"}, ctx
    )
    assert result.outcome is Outcome.empty


# --- booking + the conflict discipline -------------------------------------- #
async def test_booking_confirms_the_time_that_was_actually_written() -> None:
    ctx = await _connected_context()
    start = _window_start()
    result = await catalog.BookAppointmentTool(_backend()).execute(
        {"slot": _slot(start), "email": LEAD}, ctx
    )
    assert result.outcome is Outcome.ok
    assert result.payload["start"] == start.isoformat()
    assert result.payload["email"] == LEAD
    assert result.payload["bookingRef"]


async def test_double_booking_the_same_slot_is_failed_not_ok() -> None:
    """The whole point of read-before-write: Google would accept BOTH inserts."""
    ctx = await _connected_context()
    start = _window_start()
    tool = catalog.BookAppointmentTool(_backend())
    first = await tool.execute({"slot": _slot(start), "email": LEAD}, ctx)
    second = await tool.execute({"slot": _slot(start), "email": "other@example.com"}, ctx)

    assert first.outcome is Outcome.ok
    assert second.outcome is Outcome.failed
    assert second.reason == "slot_conflict"


async def test_a_partial_overlap_also_conflicts() -> None:
    ctx = await _connected_context()
    start = _window_start()
    tool = catalog.BookAppointmentTool(_backend())
    await tool.execute({"slot": _slot(start, minutes=60), "email": LEAD}, ctx)
    overlapping = await tool.execute(
        {"slot": _slot(start + timedelta(minutes=30)), "email": "other@example.com"}, ctx
    )
    assert overlapping.outcome is Outcome.failed
    assert overlapping.reason == "slot_conflict"


# --- reschedule (read-before-write) ----------------------------------------- #
async def test_reschedule_into_a_free_slot_moves_the_booking() -> None:
    ctx = await _connected_context()
    start = _window_start()
    booked = await catalog.BookAppointmentTool(_backend()).execute(
        {"slot": _slot(start), "email": LEAD}, ctx
    )
    target = start + timedelta(hours=1)

    moved = await catalog.RescheduleAppointmentTool(_backend()).execute(
        {"email": LEAD, "slot": _slot(target)}, ctx
    )
    assert moved.outcome is Outcome.ok
    assert moved.payload["start"] == target.isoformat()
    # Same event moved, not a second one created.
    assert moved.payload["bookingRef"] == booked.payload["bookingRef"]


async def test_reschedule_into_a_busy_slot_is_failed() -> None:
    ctx = await _connected_context()
    start = _window_start()
    book = catalog.BookAppointmentTool(_backend())
    await book.execute({"slot": _slot(start), "email": LEAD}, ctx)
    blocker = start + timedelta(hours=1)
    await book.execute({"slot": _slot(blocker), "email": "someone@example.com"}, ctx)

    result = await catalog.RescheduleAppointmentTool(_backend()).execute(
        {"email": LEAD, "slot": _slot(blocker)}, ctx
    )
    assert result.outcome is Outcome.failed
    assert result.reason == "slot_conflict"


async def test_rescheduling_onto_its_own_time_is_not_a_self_conflict() -> None:
    """The moving event must not be counted as its own blocker — freeBusy cannot make
    that distinction, which is why the pre-write check reads addressable events."""
    ctx = await _connected_context()
    start = _window_start()
    await catalog.BookAppointmentTool(_backend()).execute(
        {"slot": _slot(start), "email": LEAD}, ctx
    )
    result = await catalog.RescheduleAppointmentTool(_backend()).execute(
        {"email": LEAD, "slot": _slot(start, minutes=60)}, ctx
    )
    assert result.outcome is Outcome.ok


async def test_two_bookings_for_one_lead_are_ambiguous_never_a_guess() -> None:
    ctx = await _connected_context()
    start = _window_start()
    book = catalog.BookAppointmentTool(_backend())
    await book.execute({"slot": _slot(start), "email": LEAD}, ctx)
    await book.execute({"slot": _slot(start + timedelta(hours=2)), "email": LEAD}, ctx)

    result = await catalog.CancelAppointmentTool(_backend()).execute({"email": LEAD}, ctx)
    assert result.outcome is Outcome.ambiguous
    assert len(result.payload["candidates"]) == 2


async def test_reschedule_with_no_booking_on_file_is_empty() -> None:
    ctx = await _connected_context()
    result = await catalog.RescheduleAppointmentTool(_backend()).execute(
        {"email": LEAD, "slot": _slot(_window_start())}, ctx
    )
    assert result.outcome is Outcome.empty


# --- cancel ----------------------------------------------------------------- #
async def test_cancel_removes_the_booking_and_frees_the_slot() -> None:
    ctx = await _connected_context()
    start = _window_start()
    await catalog.BookAppointmentTool(_backend()).execute(
        {"slot": _slot(start), "email": LEAD}, ctx
    )

    cancelled = await catalog.CancelAppointmentTool(_backend()).execute({"email": LEAD}, ctx)
    assert cancelled.outcome is Outcome.ok
    assert cancelled.payload["status"] == "cancelled"

    # The freed slot is bookable again — proof the delete reached the calendar.
    rebooked = await catalog.BookAppointmentTool(_backend()).execute(
        {"slot": _slot(start), "email": "other@example.com"}, ctx
    )
    assert rebooked.outcome is Outcome.ok


# --- no credential ---------------------------------------------------------- #
@pytest.mark.parametrize(
    ("tool_factory", "inputs"),
    [
        (catalog.CheckAvailabilityTool, {"window": "2026-08-03"}),
        (catalog.BookAppointmentTool, {"slot": "2026-08-03T09:00:00+00:00", "email": LEAD}),
        (catalog.RescheduleAppointmentTool, {"email": LEAD, "slot": "2026-08-03T09:00:00+00:00"}),
        (catalog.CancelAppointmentTool, {"email": LEAD}),
    ],
)
async def test_missing_token_is_a_structured_failure_never_a_crash_or_empty(
    tool_factory: type, inputs: dict[str, object]
) -> None:
    """A connection marked connected with no stored token must not read as "no slots"."""
    ctx = ToolContext(
        admin_id=uuid.uuid4(), instance_id=uuid.uuid4(), connection_status="connected"
    )
    result = await tool_factory(_backend()).execute(inputs, ctx)
    assert result.outcome is Outcome.failed
    assert result.reason == "calendar_error: CalendarUnavailable"


async def test_one_tenants_token_never_sees_another_tenants_calendar() -> None:
    first, second = await _connected_context(), await _connected_context()
    start = _window_start()
    await catalog.BookAppointmentTool(_backend()).execute(
        {"slot": _slot(start), "email": LEAD}, first
    )

    # The same slot is untouched for the other tenant.
    result = await catalog.BookAppointmentTool(_backend()).execute(
        {"slot": _slot(start), "email": LEAD}, second
    )
    assert result.outcome is Outcome.ok
