"""Round 7 M10 scheduling follow-ups, over the real in-memory transports.

* ``slot_in_past`` — a book or a move into a time already gone used to come back as
  ``slot_conflict``; the model, given no reason for it, told the customer "someone
  just took that time". Both backends now refuse it before any write (on the
  business's clock, with a small clock-skew tolerance), the catalog returns
  ``failed("slot_in_past")``, and the read-back copy the model reads with it says the
  time has already passed and asks for another.
* Calendly's link end — derived from the generic 30-minute default whatever the event
  type was; now the event type's OWN length (on file, else asked; never guessed).
* No business time zone — every time a tool proposes or reads back carries the
  explicit label ``UTC``, and the Today card gains ``timezone_unset`` (never a guess).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from app.connections.secrets import LocalSecretStore, get_secret_store, secret_ref_for
from app.employee.status import TIME_BOUND_TOOLS, derive_needs
from app.runtime.orchestrator import Orchestrator
from app.tools import calendar_backend, catalog
from app.tools import calendly_backend as cb
from app.tools import contract as c
from app.tools.calendar_backend import (
    PAST_SLOT_TOLERANCE,
    GoogleCalendarBackend,
    InMemoryCalendarApi,
)
from app.tools.contract import Outcome, ToolContext

from tests.unit.test_employee_needs import _snap

TORONTO = "America/Toronto"
# Tuesday 6 October 2026, 2:00 pm in Toronto (EDT, UTC-4) = 18:00 UTC.
NOW = datetime(2026, 10, 6, 18, 0, tzinfo=UTC)
LEAD = "dana@lead.example"
EVENT_TYPE = "https://api.calendly.com/event_types/ET-m10"
USER = "https://api.calendly.com/users/U-m10"


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch):
    LocalSecretStore.reset()
    InMemoryCalendarApi.reset()
    cb.InMemoryCalendlyApi.reset()
    monkeypatch.setattr(calendar_backend, "now_utc", lambda: NOW)
    yield
    InMemoryCalendarApi.reset()
    cb.InMemoryCalendlyApi.reset()


async def _google_ctx(timezone: str | None = TORONTO) -> ToolContext:
    admin, instance = uuid.uuid4(), uuid.uuid4()
    await get_secret_store().put(
        secret_ref_for(admin, instance, "calendar"), {"access_token": f"tok-{admin}"}
    )
    return ToolContext(
        admin_id=admin, instance_id=instance, connection_status="connected", timezone=timezone
    )


class _Calendly(cb.CalendlyBackend):
    """Credentials from the test, not the database."""

    def __init__(self, api: cb.CalendlyApi, *, minutes_on_file: int | None = None) -> None:
        super().__init__(api)
        self.minutes_on_file = minutes_on_file

    async def _credentials(self, ctx: ToolContext) -> cb.CalendlyCredentials:
        return cb.CalendlyCredentials(
            access_token="tok",
            user_uri=USER,
            event_type_uri=EVENT_TYPE,
            event_duration_minutes=self.minutes_on_file,
        )


class _CountingCalendly(cb.InMemoryCalendlyApi):
    def __init__(self) -> None:
        self.type_lookups = 0

    async def event_types(self, cred, user_uri):
        self.type_lookups += 1
        return await super().event_types(cred, user_uri)


def _event_type(minutes: int) -> cb.CalendlyEventType:
    return cb.CalendlyEventType(
        uri=EVENT_TYPE,
        name=f"{minutes} minute consultation",
        active=True,
        duration_minutes=minutes,
        scheduling_url="https://calendly.com/acme/consult",
    )


# --- slot_in_past: Google -------------------------------------------------------------


async def test_google_a_past_slot_is_slot_in_past_on_the_business_clock_and_never_written():
    ctx = await _google_ctx()
    book = catalog.BookAppointmentTool(GoogleCalendarBackend())
    # 1 pm in Toronto is an hour ago: refused, with the reason the model can read back.
    past = await book.execute({"slot": "2026-10-06T13:00:00", "email": LEAD}, ctx)
    assert past.outcome is Outcome.failed
    assert past.reason == catalog.SLOT_IN_PAST
    # 3 pm, offset-less, is 3 pm in Toronto — still ahead (as 15:00 UTC it would be past).
    later = await book.execute({"slot": "2026-10-06T15:00:00", "email": LEAD}, ctx)
    assert later.outcome is Outcome.ok, later.reason
    assert later.payload["start"] == "2026-10-06T15:00:00-04:00"
    # The refusal wrote nothing: only the 3 pm booking is on the calendar.
    events = await InMemoryCalendarApi().events_in(
        calendar_backend.CalendarCredentials(access_token=f"tok-{ctx.admin_id}"),
        NOW - timedelta(days=1),
        NOW + timedelta(days=1),
    )
    assert [e.start for e in events] == [datetime(2026, 10, 6, 19, 0, tzinfo=UTC)]


async def test_google_the_clock_skew_tolerance_admits_a_slot_starting_this_minute():
    ctx = await _google_ctx()
    book = catalog.BookAppointmentTool(GoogleCalendarBackend())
    just_started = (NOW - timedelta(minutes=1)).isoformat()
    ok = await book.execute({"slot": just_started, "email": LEAD}, ctx)
    assert ok.outcome is Outcome.ok, ok.reason
    beyond = (NOW - PAST_SLOT_TOLERANCE - timedelta(seconds=30)).isoformat()
    gone = await book.execute({"slot": beyond, "email": "other@lead.example"}, ctx)
    assert gone.reason == catalog.SLOT_IN_PAST


async def test_google_a_move_into_the_past_is_slot_in_past_and_the_booking_stays():
    ctx = await _google_ctx()
    first = await catalog.BookAppointmentTool(GoogleCalendarBackend()).execute(
        {"slot": "2026-10-08T15:00:00", "email": LEAD}, ctx
    )
    assert first.outcome is Outcome.ok, first.reason
    moved = await catalog.RescheduleAppointmentTool(GoogleCalendarBackend()).execute(
        {"email": LEAD, "slot": "2026-10-06T09:00:00"}, ctx
    )
    assert moved.outcome is Outcome.failed
    assert moved.reason == catalog.SLOT_IN_PAST
    (still,) = await GoogleCalendarBackend().find_bookings(ctx, email=LEAD)
    assert still["start"] == "2026-10-08T15:00:00-04:00"


# --- slot_in_past: Calendly -----------------------------------------------------------


async def test_calendly_a_past_slot_and_a_move_into_the_past_are_slot_in_past():
    ctx = ToolContext(admin_id=uuid.uuid4(), instance_id=uuid.uuid4(), timezone=TORONTO)
    api = _CountingCalendly()
    past = await catalog.BookAppointmentTool(_Calendly(api)).execute(
        {"slot": "2026-10-06T09:00:00", "email": LEAD}, ctx
    )
    assert past.outcome is Outcome.failed
    assert past.reason == catalog.SLOT_IN_PAST

    event = cb.new_mock_event(start=datetime(2026, 10, 8, 19, 0, tzinfo=UTC), invitee_email=LEAD)
    cb.InMemoryCalendlyApi.seed_event(event)
    moved = await catalog.RescheduleAppointmentTool(_Calendly(api)).execute(
        {"email": LEAD, "slot": "2026-10-05T10:00:00"}, ctx
    )
    assert moved.outcome is Outcome.failed
    assert moved.reason == catalog.SLOT_IN_PAST

    # Started seconds ago (inside the skew tolerance): Calendly no longer offers it —
    # that stays a conflict, never a link to a time already running.
    started = await catalog.BookAppointmentTool(_Calendly(api)).execute(
        {"slot": (NOW - timedelta(seconds=30)).isoformat(), "email": LEAD}, ctx
    )
    assert started.reason == catalog.SLOT_CONFLICT


# --- the read-back copy ---------------------------------------------------------------


def test_the_read_back_copy_says_the_time_has_passed_and_asks_for_another():
    copy = catalog.failure_readback(catalog.SLOT_IN_PAST)
    assert copy is not None
    assert "already passed" in copy and "ask which other time" in copy
    assert catalog.failure_readback("slot_conflict") != copy
    assert catalog.failure_readback("calendar_error: TimeoutError") is None

    # What the model actually reads back for the failed dispatch.
    body = json.loads(
        Orchestrator._tool_result_feedback(
            {"tool_id": "book_appointment"}, c.failed(catalog.SLOT_IN_PAST)
        )
    )
    assert body == {
        "tool": "book_appointment",
        "outcome": "failed",
        "reason": "slot_in_past",
        "means": copy,
    }


# --- Calendly: the event type's own length --------------------------------------------


async def test_calendly_the_link_end_is_the_event_types_own_length():
    cb.InMemoryCalendlyApi.seed_event_types(USER, [_event_type(60)])
    three_pm = datetime(2026, 10, 8, 19, 0, tzinfo=UTC)
    cb.InMemoryCalendlyApi.seed_availability(EVENT_TYPE, [three_pm])
    api = _CountingCalendly()
    ctx = ToolContext(admin_id=uuid.uuid4(), instance_id=uuid.uuid4(), timezone=TORONTO)

    link = await _Calendly(api).book(ctx, "2026-10-08T15:00:00", LEAD)
    assert link is not None
    assert link["start"] == "2026-10-08T15:00:00-04:00"
    assert link["end"] == "2026-10-08T16:00:00-04:00"  # 60 minutes, not the 30 default
    assert api.type_lookups == 1

    # A length on file (recorded when the owner chose the event type) is used as is.
    on_file = await _Calendly(api, minutes_on_file=45).book(ctx, "2026-10-08T15:00:00", LEAD)
    assert on_file is not None and on_file["end"] == "2026-10-08T15:45:00-04:00"
    assert api.type_lookups == 1

    # The reschedule link's requested end follows the same length.
    event = cb.new_mock_event(start=three_pm, invitee_email=LEAD, minutes=60)
    cb.InMemoryCalendlyApi.seed_event(event)
    moved = await _Calendly(api).reschedule(ctx, event.event_uuid, "2026-10-09T10:00:00")
    assert moved is not None
    assert moved["requestedEnd"] == "2026-10-09T11:00:00-04:00"


async def test_calendly_with_no_way_to_know_the_length_names_no_end():
    class _NoTypes(cb.InMemoryCalendlyApi):
        async def event_types(self, cred, user_uri):
            raise cb.CalendlyUnavailable("calendly_http_503")

    cb.InMemoryCalendlyApi.seed_availability(EVENT_TYPE, [datetime(2026, 10, 8, 19, 0, tzinfo=UTC)])
    ctx = ToolContext(admin_id=uuid.uuid4(), instance_id=uuid.uuid4(), timezone=TORONTO)
    link = await _Calendly(_NoTypes()).book(ctx, "2026-10-08T15:00:00", LEAD)
    assert link is not None
    assert "end" not in link  # never a guessed end
    assert link["schedulingLink"]


# --- no business time zone: the UTC label ----------------------------------------------


async def test_without_a_zone_every_proposed_and_read_back_time_is_labelled_utc():
    ctx = await _google_ctx(timezone=None)
    offered = await catalog.CheckAvailabilityTool(GoogleCalendarBackend()).execute(
        {"window": "2026-10-07"}, ctx
    )
    assert offered.outcome is Outcome.ok
    assert offered.payload["timezone"] == "UTC"
    assert offered.payload["slots"][0] == "2026-10-07T00:00:00+00:00"
    booked = await catalog.BookAppointmentTool(GoogleCalendarBackend()).execute(
        {"slot": offered.payload["slots"][0], "email": LEAD}, ctx
    )
    assert booked.outcome is Outcome.ok, booked.reason
    assert booked.payload["timezone"] == "UTC"
    nothing = await catalog.CheckAvailabilityTool(GoogleCalendarBackend()).execute(
        {"window": "2026-10-05"}, ctx
    )
    assert nothing.outcome is Outcome.empty
    assert nothing.payload["timezone"] == "UTC"

    # With a zone on file the business's zone is named instead.
    zoned = await catalog.CheckAvailabilityTool(GoogleCalendarBackend()).execute(
        {"window": "2026-10-07"}, await _google_ctx()
    )
    assert zoned.payload["timezone"] == TORONTO

    # Calendly reads back the same label.
    cb.InMemoryCalendlyApi.seed_availability(EVENT_TYPE, [datetime(2026, 10, 8, 15, 0, tzinfo=UTC)])
    cal_ctx = ToolContext(admin_id=uuid.uuid4(), instance_id=uuid.uuid4(), timezone=None)
    link = await _Calendly(cb.InMemoryCalendlyApi()).book(cal_ctx, "2026-10-08T15:00:00", LEAD)
    assert link is not None and link["timezone"] == "UTC"


# --- no business time zone: the Today card need ----------------------------------------


def _need(snap):
    return next((n for n in derive_needs(snap) if n.code == "timezone_unset"), None)


def test_the_timezone_unset_need_appears_without_a_zone_and_disappears_with_one():
    zoneless = _snap(timezone=None, tools_enabled={"push_to_crm": True})
    need = _need(zoneless)
    assert need is not None
    assert need.href == "/dashboard/configure"
    assert "time zone" in need.detail and "UTC" in need.detail
    assert need.severity == "info"  # no time-bound tool is on

    # A time-bound tool on (a booking in the wrong hour is real) → attention.
    booking_on = replace(zoneless, tools_enabled={"book_appointment": True})
    assert "book_appointment" in TIME_BOUND_TOOLS
    assert _need(booking_on).severity == "attention"

    # A name that is not a real IANA zone is no zone either — never guessed.
    assert _need(_snap(timezone="Mars/Olympus")) is not None
    # Set → gone.
    assert _need(_snap(timezone=TORONTO)) is None
