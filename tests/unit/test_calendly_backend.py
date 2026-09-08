"""Round 6 WP-H (F055): the Calendly backend never claims a write it cannot make.

Availability is the event type's open times; a booking is a scheduling LINK for the
exact slot (said so in the payload); rescheduling hands back the invitee's link;
cancelling is a real cancellation. Pinned over the in-memory transport with a fake
credential resolver so no database is needed."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.tools import calendly_backend as cb
from app.tools.contract import ToolContext

EVENT_TYPE = "https://api.calendly.com/event_types/ET1"
USER = "https://api.calendly.com/users/U1"
T0 = datetime(2026, 9, 10, 14, 0, tzinfo=UTC)


class _Backend(cb.CalendlyBackend):
    """Credentials come from the test, not the database."""

    async def _credentials(self, ctx: ToolContext) -> cb.CalendlyCredentials:
        return cb.CalendlyCredentials(access_token="tok", user_uri=USER, event_type_uri=EVENT_TYPE)


def _ctx() -> ToolContext:
    return ToolContext(admin_id=uuid.uuid4(), instance_id=uuid.uuid4())


@pytest.fixture(autouse=True)
def _reset():
    cb.InMemoryCalendlyApi.reset()
    yield
    cb.InMemoryCalendlyApi.reset()


async def test_availability_is_the_event_types_open_times_in_the_window():
    cb.InMemoryCalendlyApi.seed_availability(
        EVENT_TYPE, [T0, T0 + timedelta(hours=1), T0 + timedelta(days=9)]
    )
    backend = _Backend(cb.InMemoryCalendlyApi())
    window = f"{T0.isoformat()}/{(T0 + timedelta(days=1)).isoformat()}"
    slots = await backend.free_busy(_ctx(), window)
    assert slots == [T0.isoformat(), (T0 + timedelta(hours=1)).isoformat()]


async def test_booking_is_a_scheduling_link_for_the_exact_slot_never_a_write():
    cb.InMemoryCalendlyApi.seed_availability(EVENT_TYPE, [T0])
    backend = _Backend(cb.InMemoryCalendlyApi())
    booking = await backend.book(_ctx(), T0.isoformat(), "dana@lead.example")
    assert booking is not None
    assert booking["state"] == "scheduling_link"
    assert booking["schedulingLink"].startswith("https://calendly.com/")
    assert booking["start"] == T0.isoformat()
    assert "completes this link" in booking["note"]
    assert "bookingRef" not in booking  # nothing was written; there is nothing to reference
    # A slot the event type does not offer is a conflict, never a fabricated link.
    assert await backend.book(_ctx(), (T0 + timedelta(hours=2)).isoformat(), "x@y") is None


async def test_find_reschedule_and_cancel_work_on_the_customers_own_events():
    event = cb.new_mock_event(start=T0, invitee_email="Dana@lead.example")
    cb.InMemoryCalendlyApi.seed_event(event)
    backend = _Backend(cb.InMemoryCalendlyApi())

    found = await backend.find_bookings(_ctx(), email="dana@lead.example")
    assert [b["bookingRef"] for b in found] == [event.event_uuid]
    assert await backend.find_bookings(_ctx(), email="someone@else.example") == []
    assert (
        await backend.find_bookings(
            _ctx(), email="someone@else.example", booking_ref=event.event_uuid
        )
        == []
    )

    moved = await backend.reschedule(_ctx(), event.event_uuid, (T0 + timedelta(days=1)).isoformat())
    assert moved is not None
    assert moved["state"] == "reschedule_link"
    assert moved["rescheduleLink"] == event.reschedule_url
    assert moved["start"] == T0.isoformat()  # the current time stands until they complete it

    cancelled = await backend.cancel(_ctx(), event.event_uuid)
    assert cancelled is not None and cancelled["status"] == "cancelled"
    # Gone means gone: a second cancel and a reschedule of a cancelled event both refuse.
    assert await backend.cancel(_ctx(), event.event_uuid) is None
    assert await backend.reschedule(_ctx(), event.event_uuid, T0.isoformat()) is None
    assert await backend.find_bookings(_ctx(), email="dana@lead.example") == []


async def test_a_missing_event_type_is_an_honest_failure_not_no_availability():
    class _NoType(cb.CalendlyBackend):
        async def _credentials(self, ctx: ToolContext) -> cb.CalendlyCredentials:
            raise cb.CalendlyUnavailable("calendly_event_type_not_chosen")

    with pytest.raises(cb.CalendlyUnavailable, match="event_type_not_chosen"):
        await _NoType(cb.InMemoryCalendlyApi()).free_busy(_ctx(), T0.isoformat())
