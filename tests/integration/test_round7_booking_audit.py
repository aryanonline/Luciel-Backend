"""Booking mini-audit (round 7 WP-2 remainder, item 11; Customer Journey Phase 5).

The four scheduling tools end to end through the broker, on BOTH providers, over the
real in-memory transports — the production code path with only the HTTP faked:

* read-before-write: a slot open at ``check_availability`` and taken before the write
  (the owner booked it in Google directly; Calendly withdrew it) is ``failed``
  ``slot_conflict`` on both providers — never a second event, never a booking row;
* reschedule / cancel locate the booking first: a move onto a busy slot is a conflict
  and the booking stays where it was; a cancelled booking is no longer on file, so a
  second cancel or a later move answers ``empty`` rather than claiming anything;
* the durable ``booking_events`` row the morning brief and the analytics card count
  is written for a CONFIRMED booking only: once per Google ``ok``, never for a move
  or a cancel, and never for a Calendly scheduling LINK — the customer has not booked
  anything yet, and the brief used to say "1 appointment booked" for a link that was
  merely sent.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db.session import tenant_session
from app.employee.status import day_counts
from app.models.booking import BookingEvent
from app.models.connection import InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.tool_auth import InstanceToolAuthorization
from app.tools import calendly_backend as cb
from app.tools.broker import ToolBroker
from app.tools.calendar_backend import CalendarCredentials, InMemoryCalendarApi
from app.tools.contract import Outcome
from app.tools.registry import build_tool
from sqlalchemy import select

EVENT_TYPE = "https://api.calendly.com/event_types/ET-audit"
USER = "https://api.calendly.com/users/U-audit"
TOKEN = "cal-token"
LEAD = "dana@lead.example"
# A week ahead of the wall clock: find_bookings searches forward from now.
T0 = (datetime.now(UTC) + timedelta(days=7)).replace(hour=14, minute=0, second=0, microsecond=0)
SCHEDULING_TOOLS = (
    "check_availability",
    "book_appointment",
    "reschedule_appointment",
    "cancel_appointment",
)


@pytest.fixture(autouse=True)
def _reset():
    InMemoryCalendarApi.reset()
    cb.InMemoryCalendlyApi.reset()
    yield
    InMemoryCalendarApi.reset()
    cb.InMemoryCalendlyApi.reset()


async def _seed(provider: str) -> tuple[uuid.UUID, uuid.UUID]:
    admin, iid = uuid.uuid4(), uuid.uuid4()
    ref = f"vm/{admin}/{iid}/calendar"
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
        for tool_id in SCHEDULING_TOOLS:
            s.add(
                InstanceToolAuthorization(
                    id=uuid.uuid4(), admin_id=admin, instance_id=iid, tool_id=tool_id, enabled=True
                )
            )
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="calendar",
                auth_class="oauth_token",
                provider=provider,
                status="connected",
                secret_ref=ref,
                non_secret_config=(
                    {"userUri": USER, "eventTypeUri": EVENT_TYPE} if provider == "calendly" else {}
                ),
            )
        )
    from app.connections.secrets import get_secret_store

    await get_secret_store().put(ref, {"access_token": TOKEN})
    return admin, iid


async def _dispatch(admin, iid, tool_id: str, inputs: dict):
    tool = build_tool(tool_id)
    assert tool is not None
    async with tenant_session(admin) as s:
        return await ToolBroker(s).dispatch(tool, inputs, admin_id=admin, instance_id=iid)


async def _booking_rows(admin: uuid.UUID) -> list[BookingEvent]:
    async with tenant_session(admin) as s:
        return list(
            (await s.execute(select(BookingEvent).where(BookingEvent.admin_id == admin)))
            .scalars()
            .all()
        )


async def _bookings_counted(admin: uuid.UUID, iid: uuid.UUID) -> int:
    """What the morning brief / Today card would say for a day around now."""
    now = datetime.now(UTC)
    async with tenant_session(admin) as s:
        counts = await day_counts(
            s,
            admin_id=admin,
            instance_id=iid,
            start=now - timedelta(days=1),
            end=now + timedelta(days=1),
        )
    return counts.bookings


async def _owner_books_in_google_directly(start: datetime, *, email: str = "walkin@x.example"):
    """An event that reached the calendar outside Luciel — the live race."""
    await InMemoryCalendarApi().insert_event(
        CalendarCredentials(access_token=TOKEN),
        start=start,
        end=start + timedelta(minutes=30),
        email=email,
    )


def _window(start: datetime, hours: int = 3) -> str:
    return f"{start.isoformat()}/{(start + timedelta(hours=hours)).isoformat()}"


# --- Google: read-before-write, move, cancel, and the counted row ------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_google_a_slot_taken_between_the_read_and_the_write_is_a_conflict_with_no_row():
    admin, iid = await _seed("google_calendar")
    offered = await _dispatch(admin, iid, "check_availability", {"window": _window(T0)})
    assert offered.outcome is Outcome.ok, offered.reason
    assert T0.isoformat() in offered.payload["slots"]

    # Between the read and the write the owner books that hour in Google themselves.
    await _owner_books_in_google_directly(T0)

    booked = await _dispatch(
        admin, iid, "book_appointment", {"slot": T0.isoformat(), "email": LEAD}
    )
    assert booked.outcome is Outcome.failed
    assert booked.reason == "slot_conflict"
    # Nothing was written for the customer, so nothing is counted for the owner.
    assert await _booking_rows(admin) == []
    assert await _bookings_counted(admin, iid) == 0
    # The next read tells the truth: the hour is gone, the next one is open.
    again = await _dispatch(admin, iid, "check_availability", {"window": _window(T0)})
    assert T0.isoformat() not in again.payload["slots"]
    assert (T0 + timedelta(minutes=30)).isoformat() in again.payload["slots"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_google_a_move_and_a_cancel_locate_the_booking_and_add_no_row():
    admin, iid = await _seed("google_calendar")
    booked = await _dispatch(
        admin, iid, "book_appointment", {"slot": T0.isoformat(), "email": LEAD}
    )
    assert booked.outcome is Outcome.ok, booked.reason
    ref = booked.payload["bookingRef"]
    rows = await _booking_rows(admin)
    assert [r.booking_ref for r in rows] == [ref]
    assert await _bookings_counted(admin, iid) == 1

    # Move to a free hour: the confirmed payload is the time actually written.
    moved_to = T0 + timedelta(hours=1)
    moved = await _dispatch(
        admin, iid, "reschedule_appointment", {"email": LEAD, "slot": moved_to.isoformat()}
    )
    assert moved.outcome is Outcome.ok, moved.reason
    assert moved.payload["bookingRef"] == ref
    assert moved.payload["start"] == moved_to.isoformat()

    # Move onto an hour the owner just took: a conflict, and the booking stays put.
    taken = T0 + timedelta(hours=2)
    await _owner_books_in_google_directly(taken)
    refused = await _dispatch(
        admin, iid, "reschedule_appointment", {"email": LEAD, "slot": taken.isoformat()}
    )
    assert refused.outcome is Outcome.failed
    assert refused.reason == "slot_conflict"
    slots = (await _dispatch(admin, iid, "check_availability", {"window": _window(T0)})).payload
    assert T0.isoformat() in slots["slots"]  # the original hour was freed by the move
    assert moved_to.isoformat() not in slots["slots"]  # the booking still holds its new hour

    cancelled = await _dispatch(admin, iid, "cancel_appointment", {"email": LEAD})
    assert cancelled.outcome is Outcome.ok, cancelled.reason
    assert cancelled.payload["status"] == "cancelled"
    assert cancelled.payload["bookingRef"] == ref
    assert (
        moved_to.isoformat()
        in (await _dispatch(admin, iid, "check_availability", {"window": _window(T0)})).payload[
            "slots"
        ]
    )

    # Gone means gone: no second cancel, no move of a booking that is not there.
    assert (await _dispatch(admin, iid, "cancel_appointment", {"email": LEAD})).outcome is (
        Outcome.empty
    )
    assert (
        await _dispatch(
            admin, iid, "reschedule_appointment", {"email": LEAD, "slot": T0.isoformat()}
        )
    ).outcome is Outcome.empty

    # One booking happened; a move and a cancel are not bookings.
    assert [r.booking_ref for r in await _booking_rows(admin)] == [ref]
    assert await _bookings_counted(admin, iid) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_google_two_leads_cannot_hold_one_hour_and_only_the_first_is_counted():
    admin, iid = await _seed("google_calendar")
    first = await _dispatch(admin, iid, "book_appointment", {"slot": T0.isoformat(), "email": LEAD})
    second = await _dispatch(
        admin, iid, "book_appointment", {"slot": T0.isoformat(), "email": "other@lead.example"}
    )
    assert first.outcome is Outcome.ok, first.reason
    assert second.outcome is Outcome.failed
    assert second.reason == "slot_conflict"
    assert [r.booking_ref for r in await _booking_rows(admin)] == [first.payload["bookingRef"]]
    assert await _bookings_counted(admin, iid) == 1


# --- Calendly: the link is not a booking; withdrawn and cancelled slots ------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_calendly_scheduling_link_is_never_counted_as_a_booking():
    admin, iid = await _seed("calendly")
    cb.InMemoryCalendlyApi.seed_availability(EVENT_TYPE, [T0])
    linked = await _dispatch(
        admin, iid, "book_appointment", {"slot": T0.isoformat(), "email": LEAD}
    )
    assert linked.outcome is Outcome.ok, linked.reason
    assert linked.payload["state"] == "scheduling_link"
    assert "bookingRef" not in linked.payload
    # The customer has a link, not an appointment: nothing for the brief to count.
    assert await _booking_rows(admin) == []
    assert await _bookings_counted(admin, iid) == 0

    # Once they complete it, Calendly holds the event and Luciel can act on it.
    event = cb.new_mock_event(start=T0, invitee_email=LEAD)
    cb.InMemoryCalendlyApi.seed_event(event)
    cancelled = await _dispatch(admin, iid, "cancel_appointment", {"email": LEAD})
    assert cancelled.outcome is Outcome.ok, cancelled.reason
    assert cancelled.payload["status"] == "cancelled"
    assert await _booking_rows(admin) == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_calendly_a_slot_withdrawn_between_the_read_and_the_write_is_a_conflict():
    admin, iid = await _seed("calendly")
    later = T0 + timedelta(hours=1)
    cb.InMemoryCalendlyApi.seed_availability(EVENT_TYPE, [T0, later])
    offered = await _dispatch(admin, iid, "check_availability", {"window": _window(T0)})
    assert offered.outcome is Outcome.ok, offered.reason
    assert offered.payload["slots"] == [T0.isoformat(), later.isoformat()]

    # Someone completes T0 on the owner's page before this customer does.
    cb.InMemoryCalendlyApi.seed_availability(EVENT_TYPE, [later])

    gone = await _dispatch(admin, iid, "book_appointment", {"slot": T0.isoformat(), "email": LEAD})
    assert gone.outcome is Outcome.failed
    assert gone.reason == "slot_conflict"
    assert await _booking_rows(admin) == []
    still = await _dispatch(
        admin, iid, "book_appointment", {"slot": later.isoformat(), "email": LEAD}
    )
    assert still.outcome is Outcome.ok, still.reason
    assert still.payload["start"] == later.isoformat()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_calendly_a_cancelled_booking_is_no_longer_on_file_by_reference_either():
    """A cancelled event located by its reference used to come back as a match, so a
    move answered ``failed slot_conflict`` — about a time nobody holds. It is empty."""
    admin, iid = await _seed("calendly")
    event = cb.new_mock_event(start=T0, invitee_email=LEAD)
    cb.InMemoryCalendlyApi.seed_event(event)
    by_ref = {"email": LEAD, "booking_ref": event.event_uuid}

    cancelled = await _dispatch(admin, iid, "cancel_appointment", by_ref)
    assert cancelled.outcome is Outcome.ok, cancelled.reason

    again = await _dispatch(admin, iid, "cancel_appointment", by_ref)
    assert again.outcome is Outcome.empty
    moved = await _dispatch(
        admin,
        iid,
        "reschedule_appointment",
        {**by_ref, "slot": (T0 + timedelta(days=1)).isoformat()},
    )
    assert moved.outcome is Outcome.empty
    assert await _booking_rows(admin) == []
