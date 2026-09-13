"""Round 6 WP-H (F055): the four scheduling tools dispatch on Calendly through the
same broker gates as Google Calendar, and every outcome is the honest one."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db.session import tenant_session
from app.models.connection import InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.tool_auth import InstanceToolAuthorization
from app.tools import calendly_backend as cb
from app.tools.broker import ToolBroker
from app.tools.contract import Outcome
from app.tools.registry import build_tool

pytestmark = [pytest.mark.asyncio]

EVENT_TYPE = "https://api.calendly.com/event_types/ET-walk"
USER = "https://api.calendly.com/users/U-walk"
# A week ahead of the wall clock: find_bookings searches forward from now, so a fixed
# date went stale the day after it passed (the 2026-09-13 full suite caught it).
T0 = (datetime.now(UTC) + timedelta(days=7)).replace(minute=0, second=0, microsecond=0)
LEAD = "dana@lead.example"


@pytest.fixture(autouse=True)
def _reset():
    cb.InMemoryCalendlyApi.reset()
    yield
    cb.InMemoryCalendlyApi.reset()


async def _seed(*, event_type: str | None = EVENT_TYPE) -> tuple[uuid.UUID, uuid.UUID]:
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
        for tool_id in (
            "check_availability",
            "book_appointment",
            "reschedule_appointment",
            "cancel_appointment",
        ):
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
                provider="calendly",
                status="connected",
                secret_ref=ref,
                non_secret_config={"userUri": USER, "eventTypeUri": event_type or ""},
            )
        )
    from app.connections.secrets import get_secret_store

    await get_secret_store().put(ref, {"access_token": "cal-token"})
    return admin, iid


async def _dispatch(admin, iid, tool_id: str, inputs: dict):
    tool = build_tool(tool_id)
    assert tool is not None
    async with tenant_session(admin) as s:
        return await ToolBroker(s).dispatch(tool, inputs, admin_id=admin, instance_id=iid)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_availability_and_booking_link_through_the_broker():
    admin, iid = await _seed()
    cb.InMemoryCalendlyApi.seed_availability(EVENT_TYPE, [T0, T0 + timedelta(hours=1)])
    window = f"{T0.isoformat()}/{(T0 + timedelta(days=1)).isoformat()}"
    res = await _dispatch(admin, iid, "check_availability", {"window": window})
    assert res.outcome is Outcome.ok, res.reason
    assert res.payload["slots"] == [T0.isoformat(), (T0 + timedelta(hours=1)).isoformat()]

    booked = await _dispatch(
        admin, iid, "book_appointment", {"slot": T0.isoformat(), "email": LEAD}
    )
    assert booked.outcome is Outcome.ok, booked.reason
    assert booked.payload["state"] == "scheduling_link"
    assert booked.payload["schedulingLink"].startswith("https://calendly.com/")
    assert "completes this link" in booked.payload["note"]
    gone = await _dispatch(
        admin,
        iid,
        "book_appointment",
        {"slot": (T0 + timedelta(hours=3)).isoformat(), "email": LEAD},
    )
    assert gone.outcome is Outcome.failed and gone.reason == "slot_conflict"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_reschedule_hands_back_the_link_and_cancel_really_cancels():
    admin, iid = await _seed()
    event = cb.new_mock_event(start=T0, invitee_email=LEAD)
    cb.InMemoryCalendlyApi.seed_event(event)
    moved = await _dispatch(
        admin,
        iid,
        "reschedule_appointment",
        {"email": LEAD, "slot": (T0 + timedelta(days=1)).isoformat()},
    )
    assert moved.outcome is Outcome.ok, moved.reason
    assert moved.payload["state"] == "reschedule_link"
    assert moved.payload["rescheduleLink"] == event.reschedule_url
    cancelled = await _dispatch(admin, iid, "cancel_appointment", {"email": LEAD})
    assert cancelled.outcome is Outcome.ok, cancelled.reason
    assert cancelled.payload["status"] == "cancelled"
    # Nothing left to move: the read-before-write half answers empty, not a fake move.
    again = await _dispatch(
        admin, iid, "reschedule_appointment", {"email": LEAD, "slot": T0.isoformat()}
    )
    assert again.outcome is Outcome.empty


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_no_event_type_chosen_is_an_honest_failure():
    admin, iid = await _seed(event_type=None)
    cb.InMemoryCalendlyApi.seed_availability(EVENT_TYPE, [T0])
    res = await _dispatch(admin, iid, "check_availability", {"window": T0.isoformat()})
    assert res.outcome is Outcome.failed
    assert "calendar_error" in (res.reason or "")
