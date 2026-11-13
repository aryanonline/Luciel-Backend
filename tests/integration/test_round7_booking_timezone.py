"""The business's clock, end to end (round 7 WP-2 remainder, item 11; Journey Phase 5).

The live condition: a customer of a Toronto business texts "3pm next week". Nothing told
the model which zone the business keeps or what today's date is, and the scheduling tool
read the offset-less ``…T15:00:00`` the model produced as 15:00 UTC — an 11 am (or 10 am)
appointment on the owner's calendar, read back to the customer as a UTC hour.

Walked through the real seams, with only the HTTP faked:

* the instance row's ``timezone`` → ``build_operating_context`` → the system prompt line
  AND ``ToolBroker.dispatch(timezone=)`` → ``ToolContext.timezone`` → the registry's real
  Google tool: the event lands on the customer's 3 pm, the result the model reads back
  carries the zone's offset, and the booking is counted once;
* the same zone through the broker on Calendly: the owner's open times are offered on
  the business's clock and the offset-less time resolves to the right scheduling link;
* no zone on the row keeps the whole path on UTC (the committed audit file's cases).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from app.connections.secrets import get_secret_store
from app.db.session import tenant_session
from app.employee.context import build_operating_context
from app.employee.status import day_counts
from app.models.booking import BookingEvent
from app.models.connection import InstanceConnection
from app.models.conversation import Session as SessionModel
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.tool_auth import InstanceToolAuthorization
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import Orchestrator
from app.runtime.testing import EchoModel
from app.tools import calendly_backend as cb
from app.tools.broker import ToolBroker
from app.tools.calendar_backend import CalendarCredentials, InMemoryCalendarApi
from app.tools.contract import Outcome
from app.tools.registry import build_tool
from sqlalchemy import select

TORONTO = "America/Toronto"
ZONE = ZoneInfo(TORONTO)
EVENT_TYPE = "https://api.calendly.com/event_types/ET-clock"
USER = "https://api.calendly.com/users/U-clock"
TOKEN = "cal-token-clock"
LEAD = "dana@lead.example"
PHONE = "+15551230000"
# A week ahead on the business's own calendar, so every time below is still to come.
DAY: date = (datetime.now(ZONE) + timedelta(days=7)).date()
THREE_PM_LOCAL = datetime.combine(DAY, time(15, 0), tzinfo=ZONE)
THREE_PM_UTC = THREE_PM_LOCAL.astimezone(UTC)
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


class _RecordingEcho(EchoModel):
    """Keeps every prompt and message list the loop handed the model."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.prompts: list[str] = []
        self.conversations: list[list[dict[str, Any]]] = []

    async def complete(self, system_prompt, messages, *, tools=None):  # type: ignore[override]
        self.prompts.append(system_prompt)
        self.conversations.append([dict(m) for m in messages])
        return await super().complete(system_prompt, messages, tools=tools)


async def _empty_retriever(_q: str) -> list[str]:
    return []


async def _seed(provider: str, *, timezone: str | None) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
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
                channels=[{"id": "sms", "enabled": True}],
                tools=[],
                escalation={},
                personality={},
                timezone=timezone,
            )
        )
        await s.flush()
        s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel="sms", mode="ai"))
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
    await get_secret_store().put(ref, {"access_token": TOKEN})
    return admin, iid, sid


async def _dispatch(admin, iid, tool_id: str, inputs: dict, *, timezone: str | None):
    tool = build_tool(tool_id)
    assert tool is not None
    async with tenant_session(admin) as s:
        return await ToolBroker(s).dispatch(
            tool, inputs, admin_id=admin, instance_id=iid, timezone=timezone
        )


async def _calendar_events(start: datetime, end: datetime):
    return await InMemoryCalendarApi().events_in(
        CalendarCredentials(access_token=TOKEN), start, end
    )


async def _bookings_counted(admin: uuid.UUID, iid: uuid.UUID) -> int:
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


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_customers_three_pm_lands_on_three_pm_at_the_business_through_the_loop():
    admin, iid, sid = await _seed("google_calendar", timezone=TORONTO)
    said = f"{DAY.isoformat()}T15:00:00"  # what the model makes of "3pm" — no offset
    model = _RecordingEcho(
        confidence=1.0,
        tool_call_script=[
            [{"tool_id": "book_appointment", "inputs": {"slot": said, "email": LEAD}}]
        ],
        fixed_reply="You're booked for 3:00 pm.",
    )
    async with tenant_session(admin) as s:
        inst = (await s.execute(select(Instance).where(Instance.id == iid))).scalar_one()
        operating_context = await build_operating_context(s, inst, admin_id=admin)
        assert operating_context.timezone == TORONTO
        orch = Orchestrator(
            session=s,
            router=LLMRouter(primary=model, fallback=EchoModel()),
            retriever=_empty_retriever,
            admin_id=admin,
            instance_id=iid,
            enabled_channels={"sms"},
            tool_resolver=build_tool,
            has_payment_method=False,
            operating_context=operating_context,
        )
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="sms",
            mode="ai",
            inbound_text="3pm works, book me in please",
            transport_identifier=PHONE,
            received_at=datetime.now(UTC),
        )
        assert result.tool_outcomes == ["ok"]

    # The model was told the business's zone and what today is there.
    today = datetime.now(ZONE).strftime("%A %d %B %Y")
    assert f"The business keeps time in {TORONTO}; today there is" in model.prompts[0]
    # (Midnight can pass between the two clock reads; the date is one of the two.)
    yesterday = (datetime.now(ZONE) - timedelta(days=1)).strftime("%A %d %B %Y")
    assert any(f"today there is {d}." in model.prompts[0] for d in (today, yesterday))

    # The owner's calendar holds the customer's 3 pm — not 15:00 UTC.
    (event,) = await _calendar_events(
        THREE_PM_UTC - timedelta(days=1), THREE_PM_UTC + timedelta(days=1)
    )
    assert event.start == THREE_PM_UTC
    assert event.email == LEAD
    # The result the model read back is on the business's clock, the zone named.
    feedback = [
        str(m.get("content", "")) for m in model.conversations[-1] if m.get("role") == "tool_result"
    ]
    assert len(feedback) == 1
    assert THREE_PM_LOCAL.isoformat() in feedback[0]
    assert TORONTO in feedback[0]
    assert THREE_PM_UTC.isoformat() not in feedback[0]
    # One confirmed booking, counted once for the morning brief.
    async with tenant_session(admin) as s:
        rows = (
            (await s.execute(select(BookingEvent).where(BookingEvent.admin_id == admin)))
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert await _bookings_counted(admin, iid) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_broker_hands_the_zone_to_the_google_tools_and_utc_stays_the_default():
    admin, iid, _sid = await _seed("google_calendar", timezone=TORONTO)
    offered = await _dispatch(
        admin, iid, "check_availability", {"window": DAY.isoformat()}, timezone=TORONTO
    )
    assert offered.outcome is Outcome.ok, offered.reason
    slots = offered.payload["slots"]
    # The business's day, midnight to midnight, each time carrying the zone's offset.
    assert slots[0] == datetime.combine(DAY, time(0, 0), tzinfo=ZONE).isoformat()
    assert slots[-1] == datetime.combine(DAY, time(23, 30), tzinfo=ZONE).isoformat()
    assert THREE_PM_LOCAL.isoformat() in slots

    booked = await _dispatch(
        admin,
        iid,
        "book_appointment",
        {"slot": THREE_PM_LOCAL.isoformat(), "email": LEAD},
        timezone=TORONTO,
    )
    assert booked.outcome is Outcome.ok, booked.reason
    assert booked.payload["start"] == THREE_PM_LOCAL.isoformat()
    assert booked.payload["timezone"] == TORONTO
    assert await _bookings_counted(admin, iid) == 1

    # A caller that resolved no zone (no operating context) reads the same booking
    # back in UTC — and (round 7 M10) says so: the label is UTC, never a bare hour.
    found = await _dispatch(admin, iid, "cancel_appointment", {"email": LEAD}, timezone=None)
    assert found.outcome is Outcome.ok, found.reason
    assert found.payload["start"] == THREE_PM_UTC.isoformat()
    assert found.payload["timezone"] == "UTC"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_broker_hands_the_zone_to_the_calendly_tools():
    admin, iid, _sid = await _seed("calendly", timezone=TORONTO)
    cb.InMemoryCalendlyApi.seed_availability(EVENT_TYPE, [THREE_PM_UTC])
    offered = await _dispatch(
        admin, iid, "check_availability", {"window": DAY.isoformat()}, timezone=TORONTO
    )
    assert offered.outcome is Outcome.ok, offered.reason
    assert offered.payload["slots"] == [THREE_PM_LOCAL.isoformat()]

    linked = await _dispatch(
        admin,
        iid,
        "book_appointment",
        {"slot": f"{DAY.isoformat()}T15:00:00", "email": LEAD},
        timezone=TORONTO,
    )
    assert linked.outcome is Outcome.ok, linked.reason
    assert linked.payload["state"] == "scheduling_link"
    assert linked.payload["start"] == THREE_PM_LOCAL.isoformat()
    assert linked.payload["timezone"] == TORONTO
    # Still a link, never a booking: nothing for the brief to count.
    assert await _bookings_counted(admin, iid) == 0
