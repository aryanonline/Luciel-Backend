"""Booking mini-audit, the clock (round 7 WP-2 remainder, item 11; Customer Journey Phase 5).

Two live conditions the scheduling tools got wrong, pinned over the real in-memory
transports (the production code path, only the HTTP faked):

* **the business's zone against the customer's stated time** — a customer of a Toronto
  business says "3pm Thursday", the model passes ``2026-10-08T15:00:00``, and the tool
  read it as 15:00 **UTC**: an 11 am booking. "Check Tuesday" was the UTC day (8 pm
  Monday to 8 pm Tuesday in Toronto), and every time read back was a UTC hour. With
  ``ToolContext.timezone`` an offset-less time and a bare date are read on the business's
  clock, an explicit offset is honoured, and every returned time is on that clock with
  the zone named. No zone on file keeps the tools on UTC, byte for byte;
* **a time that has already gone** — "anything today?" asked at 2 pm offered 9 am (the
  day's window starts at midnight) and a past slot could be written; on Calendly the
  same question failed outright, because its API refuses a start_time in the past and
  the tool reported a calendar outage. A started slot is never offered, a write into
  the past is a conflict, and Calendly is asked from now on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.connections.secrets import LocalSecretStore, get_secret_store, secret_ref_for
from app.tools import calendar_backend, catalog
from app.tools import calendly_backend as cb
from app.tools.calendar_backend import (
    CalendarCredentials,
    GoogleCalendarBackend,
    InMemoryCalendarApi,
    business_zone,
    open_slots,
    parse_interval,
)
from app.tools.contract import Outcome, ToolContext
from app.tools.schemas import TOOL_SCHEMAS

TORONTO = "America/Toronto"
ZONE = ZoneInfo(TORONTO)
# Tuesday 6 October 2026, 2:00 pm in Toronto (EDT, UTC-4).
NOW = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)
LEAD = "dana@lead.example"
EVENT_TYPE = "https://api.calendly.com/event_types/ET-clock"
USER = "https://api.calendly.com/users/U-clock"


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch):
    LocalSecretStore.reset()
    InMemoryCalendarApi.reset()
    cb.InMemoryCalendlyApi.reset()
    monkeypatch.setattr(calendar_backend, "now_utc", lambda: NOW)
    yield
    InMemoryCalendarApi.reset()
    cb.InMemoryCalendlyApi.reset()


async def _google_ctx(timezone: str | None = TORONTO) -> tuple[ToolContext, CalendarCredentials]:
    """A tenant whose Google Calendar connect completed, on the business's clock."""
    admin, instance = uuid.uuid4(), uuid.uuid4()
    token = f"tok-{admin}"
    await get_secret_store().put(
        secret_ref_for(admin, instance, "calendar"), {"access_token": token}
    )
    ctx = ToolContext(
        admin_id=admin, instance_id=instance, connection_status="connected", timezone=timezone
    )
    return ctx, CalendarCredentials(access_token=token)


def _google() -> GoogleCalendarBackend:
    return GoogleCalendarBackend()


# --- the window maths -------------------------------------------------------------- #


def test_a_bare_date_is_the_business_day_not_the_utc_day() -> None:
    start, end = parse_interval("2026-10-06", tz=ZONE)
    assert start == datetime(2026, 10, 6, 4, 0, tzinfo=UTC)  # midnight in Toronto
    assert end == datetime(2026, 10, 7, 4, 0, tzinfo=UTC)
    # No zone: the UTC day, exactly as before a zone could be set.
    assert parse_interval("2026-10-06") == (
        datetime(2026, 10, 6, 0, 0, tzinfo=UTC),
        datetime(2026, 10, 7, 0, 0, tzinfo=UTC),
    )


def test_a_local_day_is_23_or_25_hours_across_a_clock_change() -> None:
    spring_start, spring_end = parse_interval("2026-03-08", tz=ZONE)
    assert spring_end - spring_start == timedelta(hours=23)
    autumn_start, autumn_end = parse_interval("2026-11-01", tz=ZONE)
    assert autumn_end - autumn_start == timedelta(hours=25)


def test_an_offsetless_time_is_the_business_clock_and_an_explicit_offset_is_honoured() -> None:
    # "3pm Thursday" at a Toronto business is 19:00 UTC, not 15:00 UTC.
    assert parse_interval("2026-10-08T15:00:00", tz=ZONE)[0] == datetime(
        2026, 10, 8, 19, 0, tzinfo=UTC
    )
    # After the clocks go back the same wall time is an hour later in UTC.
    assert parse_interval("2026-11-02T15:00:00", tz=ZONE)[0] == datetime(
        2026, 11, 2, 20, 0, tzinfo=UTC
    )
    # A customer who named their own zone (the model passes its offset) is taken at
    # their word: 3 pm in Vancouver is 6 pm in Toronto.
    assert parse_interval("2026-10-08T15:00:00-07:00", tz=ZONE)[0] == datetime(
        2026, 10, 8, 22, 0, tzinfo=UTC
    )
    # A slot handed back exactly as check_availability returned it round-trips.
    assert parse_interval("2026-10-08T15:00:00-04:00", tz=ZONE)[0] == datetime(
        2026, 10, 8, 19, 0, tzinfo=UTC
    )
    start, end = parse_interval("2026-10-08T15:00:00/2026-10-08T17:00:00", tz=ZONE)
    assert (start, end) == (
        datetime(2026, 10, 8, 19, 0, tzinfo=UTC),
        datetime(2026, 10, 8, 21, 0, tzinfo=UTC),
    )


def test_an_unknown_or_missing_zone_keeps_the_tools_on_utc() -> None:
    assert business_zone(None) is UTC
    assert business_zone("  ") is UTC
    assert business_zone("Mars/Olympus_Mons") is UTC
    assert business_zone(f" {TORONTO} ") == ZONE


def test_open_slots_leaves_out_a_slot_that_has_already_started() -> None:
    start = datetime(2026, 10, 6, 17, 0, tzinfo=UTC)
    slots = open_slots(
        [], window_start=start, window_end=start + timedelta(hours=2), tz=ZONE, not_before=NOW
    )
    # 1:00 and 1:30 pm are gone at 2:00 pm; 2:00 and 2:30 pm are still ahead.
    assert slots == ["2026-10-06T14:00:00-04:00", "2026-10-06T14:30:00-04:00"]


def test_the_window_description_no_longer_tells_the_model_everything_is_utc() -> None:
    desc = TOOL_SCHEMAS["check_availability"]["input_schema"]["properties"]["window"]["description"]
    assert not desc.startswith("UTC ")
    assert "read on the business's clock" in desc
    assert "as UTC otherwise" in desc


# --- Google: the customer's 3 pm is the business's 3 pm ------------------------------ #


async def test_google_three_pm_is_booked_at_three_pm_at_the_business() -> None:
    ctx, cred = await _google_ctx()
    booked = await catalog.BookAppointmentTool(_google()).execute(
        {"slot": "2026-10-08T15:00:00", "email": LEAD}, ctx
    )
    assert booked.outcome is Outcome.ok, booked.reason
    # What the customer is told: their 3 pm, on the business's clock, the zone named.
    assert booked.payload["start"] == "2026-10-08T15:00:00-04:00"
    assert booked.payload["end"] == "2026-10-08T15:30:00-04:00"
    assert booked.payload["timezone"] == TORONTO
    # What the owner's calendar holds: 3 pm Toronto is 19:00 UTC — not 15:00 UTC (11 am).
    event = await InMemoryCalendarApi().get_event(cred, booked.payload["bookingRef"])
    assert event is not None
    assert event.start == datetime(2026, 10, 8, 19, 0, tzinfo=UTC)


async def test_google_a_day_is_that_day_at_the_business_on_its_own_clock() -> None:
    ctx, cred = await _google_ctx()
    # The owner has a 3 pm (Toronto) appointment of their own on the Wednesday.
    await InMemoryCalendarApi().insert_event(
        cred,
        start=datetime(2026, 10, 7, 19, 0, tzinfo=UTC),
        end=datetime(2026, 10, 7, 19, 30, tzinfo=UTC),
        email="walkin@x.example",
    )
    result = await catalog.CheckAvailabilityTool(_google()).execute({"window": "2026-10-07"}, ctx)
    assert result.outcome is Outcome.ok
    slots = result.payload["slots"]
    # Midnight to midnight in Toronto, every time carrying the zone's offset.
    assert slots[0] == "2026-10-07T00:00:00-04:00"
    assert slots[-1] == "2026-10-07T23:30:00-04:00"
    assert len(slots) == 47
    assert "2026-10-07T15:00:00-04:00" not in slots
    assert "2026-10-07T15:30:00-04:00" in slots
    # A slot goes back to the tool exactly as it was offered and lands on that hour.
    booked = await catalog.BookAppointmentTool(_google()).execute(
        {"slot": "2026-10-07T15:30:00-04:00", "email": LEAD}, ctx
    )
    assert booked.outcome is Outcome.ok, booked.reason
    assert booked.payload["start"] == "2026-10-07T15:30:00-04:00"


async def test_google_today_never_offers_the_part_of_the_day_already_gone() -> None:
    ctx, _cred = await _google_ctx()
    result = await catalog.CheckAvailabilityTool(_google()).execute({"window": "2026-10-06"}, ctx)
    assert result.outcome is Outcome.ok
    slots = result.payload["slots"]
    # It is 2:00 pm: this morning is not on offer, the rest of today is.
    assert slots[0] == "2026-10-06T14:00:00-04:00"
    assert slots[-1] == "2026-10-06T23:30:00-04:00"
    assert all(datetime.fromisoformat(slot) >= NOW for slot in slots)
    # A day that is wholly behind us has nothing to offer: empty, not failed.
    gone = await catalog.CheckAvailabilityTool(_google()).execute({"window": "2026-10-05"}, ctx)
    assert gone.outcome is Outcome.empty


async def test_google_a_time_already_gone_is_a_conflict_never_a_write() -> None:
    ctx, cred = await _google_ctx()
    whole_week = (NOW - timedelta(days=3), NOW + timedelta(days=7))
    past = await catalog.BookAppointmentTool(_google()).execute(
        {"slot": "2026-10-06T09:00:00", "email": LEAD}, ctx
    )
    assert past.outcome is Outcome.failed
    assert past.reason == "slot_conflict"
    assert await InMemoryCalendarApi().events_in(cred, *whole_week) == []

    booked = await catalog.BookAppointmentTool(_google()).execute(
        {"slot": "2026-10-08T15:00:00", "email": LEAD}, ctx
    )
    assert booked.outcome is Outcome.ok, booked.reason
    # A move into this morning is not a move; the booking stays where it was.
    moved = await catalog.RescheduleAppointmentTool(_google()).execute(
        {"email": LEAD, "slot": "2026-10-06T10:00:00"}, ctx
    )
    assert moved.outcome is Outcome.failed
    assert moved.reason == "slot_conflict"
    (event,) = await InMemoryCalendarApi().events_in(cred, *whole_week)
    assert event.start == datetime(2026, 10, 8, 19, 0, tzinfo=UTC)


async def test_google_a_move_a_cancel_and_the_candidates_read_back_on_the_business_clock() -> None:
    ctx, cred = await _google_ctx()
    first = await catalog.BookAppointmentTool(_google()).execute(
        {"slot": "2026-10-08T15:00:00", "email": LEAD}, ctx
    )
    assert first.outcome is Outcome.ok, first.reason
    ref = first.payload["bookingRef"]

    # Across the clock change: 10:30 am on 2 November is EST (UTC-5), 15:30 UTC.
    moved = await catalog.RescheduleAppointmentTool(_google()).execute(
        {"email": LEAD, "slot": "2026-11-02T10:30:00"}, ctx
    )
    assert moved.outcome is Outcome.ok, moved.reason
    assert moved.payload["bookingRef"] == ref
    assert moved.payload["start"] == "2026-11-02T10:30:00-05:00"
    assert moved.payload["timezone"] == TORONTO
    event = await InMemoryCalendarApi().get_event(cred, ref)
    assert event is not None
    assert event.start == datetime(2026, 11, 2, 15, 30, tzinfo=UTC)

    # A second booking makes "cancel my appointment" ambiguous: the candidates the
    # customer is asked to choose between are on the business's clock too.
    second = await catalog.BookAppointmentTool(_google()).execute(
        {"slot": "2026-10-09T09:00:00", "email": LEAD}, ctx
    )
    assert second.outcome is Outcome.ok, second.reason
    which = await catalog.CancelAppointmentTool(_google()).execute({"email": LEAD}, ctx)
    assert which.outcome is Outcome.ambiguous
    assert sorted(candidate["start"] for candidate in which.payload["candidates"]) == [
        "2026-10-09T09:00:00-04:00",
        "2026-11-02T10:30:00-05:00",
    ]

    cancelled = await catalog.CancelAppointmentTool(_google()).execute(
        {"email": LEAD, "booking_ref": ref}, ctx
    )
    assert cancelled.outcome is Outcome.ok, cancelled.reason
    assert cancelled.payload["status"] == "cancelled"
    assert cancelled.payload["start"] == "2026-11-02T10:30:00-05:00"
    assert cancelled.payload["timezone"] == TORONTO


async def test_google_without_a_zone_on_file_everything_stays_utc() -> None:
    ctx, cred = await _google_ctx(timezone=None)
    booked = await catalog.BookAppointmentTool(_google()).execute(
        {"slot": "2026-10-08T15:00:00", "email": LEAD}, ctx
    )
    assert booked.outcome is Outcome.ok, booked.reason
    assert booked.payload["start"] == "2026-10-08T15:00:00+00:00"
    assert "timezone" not in booked.payload
    event = await InMemoryCalendarApi().get_event(cred, booked.payload["bookingRef"])
    assert event is not None
    assert event.start == datetime(2026, 10, 8, 15, 0, tzinfo=UTC)
    result = await catalog.CheckAvailabilityTool(_google()).execute({"window": "2026-10-07"}, ctx)
    assert result.payload["slots"][0] == "2026-10-07T00:00:00+00:00"
    assert len(result.payload["slots"]) == 48


# --- Calendly: the same clock, and a question about today is not an outage ----------- #


class _RefusesThePast(cb.InMemoryCalendlyApi):
    """Calendly's own rule: a start_time that is not in the future is HTTP 400."""

    def __init__(self) -> None:
        self.asked: list[tuple[datetime, datetime]] = []

    async def available_times(
        self, cred: cb.CalendlyCredentials, event_type_uri: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, str]]:
        self.asked.append((start, end))
        if start <= NOW:
            raise cb.CalendlyUnavailable("calendly_http_400")
        return await super().available_times(cred, event_type_uri, start, end)


class _Calendly(cb.CalendlyBackend):
    """Credentials come from the test, not the database."""

    async def _credentials(self, ctx: ToolContext) -> cb.CalendlyCredentials:
        return cb.CalendlyCredentials(access_token="tok", user_uri=USER, event_type_uri=EVENT_TYPE)


def _calendly_ctx(timezone: str | None = TORONTO) -> ToolContext:
    return ToolContext(admin_id=uuid.uuid4(), instance_id=uuid.uuid4(), timezone=timezone)


async def test_calendly_slots_and_the_link_are_on_the_business_clock() -> None:
    three_pm = datetime(2026, 10, 8, 19, 0, tzinfo=UTC)
    cb.InMemoryCalendlyApi.seed_availability(EVENT_TYPE, [three_pm, three_pm + timedelta(hours=1)])
    backend = _Calendly(cb.InMemoryCalendlyApi())
    ctx = _calendly_ctx()

    slots = await backend.free_busy(ctx, "2026-10-08")
    assert slots == ["2026-10-08T15:00:00-04:00", "2026-10-08T16:00:00-04:00"]

    # The customer's "3pm", passed without an offset, is the 3 pm the owner offers.
    link = await backend.book(ctx, "2026-10-08T15:00:00", LEAD)
    assert link is not None
    assert link["state"] == "scheduling_link"
    assert link["start"] == "2026-10-08T15:00:00-04:00"
    assert link["timezone"] == TORONTO
    # And the slot string exactly as it was offered resolves to the same link.
    again = await backend.book(ctx, slots[0], LEAD)
    assert again is not None
    assert again["schedulingLink"] == link["schedulingLink"]
    # 3 pm UTC is 11 am in Toronto — not a time the owner offers.
    assert await backend.book(ctx, "2026-10-08T15:00:00+00:00", LEAD) is None


async def test_calendly_today_is_asked_from_now_on_never_from_a_past_start() -> None:
    four_pm = NOW + timedelta(hours=2)
    cb.InMemoryCalendlyApi.seed_availability(EVENT_TYPE, [four_pm])
    api = _RefusesThePast()
    backend = _Calendly(api)
    ctx = _calendly_ctx()

    # "Anything today?" at 2 pm: the window began at midnight, which Calendly refuses.
    slots = await backend.free_busy(ctx, "2026-10-06")
    assert slots == ["2026-10-06T16:00:00-04:00"]
    assert len(api.asked) == 1
    assert api.asked[0][0] > NOW

    # Yesterday has nothing to offer and Calendly is not asked.
    assert await backend.free_busy(ctx, "2026-10-05") == []
    # This morning cannot be booked and Calendly is not asked.
    assert await backend.book(ctx, "2026-10-06T09:00:00", LEAD) is None
    assert len(api.asked) == 1


async def test_calendly_an_existing_booking_reads_back_on_the_business_clock() -> None:
    event = cb.new_mock_event(start=datetime(2026, 10, 8, 19, 0, tzinfo=UTC), invitee_email=LEAD)
    cb.InMemoryCalendlyApi.seed_event(event)
    backend = _Calendly(cb.InMemoryCalendlyApi())
    ctx = _calendly_ctx()

    (found,) = await backend.find_bookings(ctx, email=LEAD)
    assert found["start"] == "2026-10-08T15:00:00-04:00"
    assert found["timezone"] == TORONTO

    moved = await backend.reschedule(ctx, event.event_uuid, "2026-10-09T10:00:00")
    assert moved is not None
    assert moved["start"] == "2026-10-08T15:00:00-04:00"  # stands until they complete it
    assert moved["requestedStart"] == "2026-10-09T10:00:00-04:00"

    cancelled = await backend.cancel(ctx, event.event_uuid)
    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
    assert cancelled["start"] == "2026-10-08T15:00:00-04:00"


async def test_calendly_without_a_zone_on_file_everything_stays_utc() -> None:
    three_pm_utc = datetime(2026, 10, 8, 15, 0, tzinfo=UTC)
    cb.InMemoryCalendlyApi.seed_availability(EVENT_TYPE, [three_pm_utc])
    backend = _Calendly(cb.InMemoryCalendlyApi())
    ctx = _calendly_ctx(timezone=None)
    assert await backend.free_busy(ctx, "2026-10-08") == ["2026-10-08T15:00:00+00:00"]
    link = await backend.book(ctx, "2026-10-08T15:00:00", LEAD)
    assert link is not None
    assert link["start"] == "2026-10-08T15:00:00+00:00"
    assert "timezone" not in link
