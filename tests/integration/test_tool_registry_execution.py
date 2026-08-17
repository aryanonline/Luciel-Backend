"""Tools are genuinely executable end-to-end (Arch §3.3.2/§3.3.3, Vision §3.2).

Three things used to be missing and are asserted here:
  * nothing in ``app/`` built the Tool objects, so the orchestrator's resolver was None
    in production and every proposed call short-circuited to ``failed``;
  * nothing wrote ``instance_tool_authorizations``, so a tool switched on in the
    dashboard was still refused ``not_authorized`` at gate 1;
  * the connection-gated tools had no backend at all, which must surface as a
    structured ``failed`` — never ``empty`` (that would read to the lead as "no slots").
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from app.db.session import tenant_session
from app.models.connection import InstanceConnection
from app.models.deferred import DeferredAction
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.tool_auth import InstanceToolAuthorization
from app.tools.authorization import sync_tool_authorizations
from app.tools.broker import ToolBroker
from app.tools.contract import Outcome
from app.tools.registry import CONNECTION_GATED_TOOL_IDS, build_tool
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]


@dataclass
class _Toggle:
    """The ``AddonToolIn`` shape ``sync_tool_authorizations`` reads (id + enabled)."""

    id: str
    enabled: bool


async def _seed(
    admin: uuid.UUID,
    *,
    authorize: list[str] | None = None,
    channels: list[dict[str, object]] | None = None,
    connections: list[str] | None = None,
    connection_providers: dict[str, str] | None = None,
) -> uuid.UUID:
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
                channels=list(channels or []),
                tools=[],
                escalation={},
                personality={},
            )
        )
        for tool_id in authorize or []:
            s.add(
                InstanceToolAuthorization(
                    id=uuid.uuid4(), admin_id=admin, instance_id=iid, tool_id=tool_id, enabled=True
                )
            )
        for conn_type in connections or []:
            s.add(
                InstanceConnection(
                    connection_id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    connection_type=conn_type,
                    auth_class="api_key",
                    provider=(connection_providers or {}).get(conn_type, "platform"),
                    status="connected",
                    # BYO (§3.1.4): an sms_sender with no designated number has nothing to
                    # send FROM, so the tool correctly refuses. Seed the designation.
                    non_secret_config=(
                        {"destination": "+15550009999"} if conn_type == "sms_sender" else None
                    ),
                )
            )
        await s.flush()
        return iid


# --- gate 1: enabling a tool in the dashboard actually authorizes it -------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_authorization_write_flips_gate_1_from_deny_to_permit() -> None:
    admin = uuid.uuid4()
    iid = await _seed(admin)  # nothing authorized yet
    tool = build_tool("schedule_callback")
    assert tool is not None

    async with tenant_session(admin) as s:
        denied = await ToolBroker(s).dispatch(
            tool,
            {"fire_at": "2026-08-01T10:00Z", "channel": "email", "message": "hi"},
            admin_id=admin,
            instance_id=iid,
        )
        assert denied.outcome is Outcome.failed
        assert denied.reason == "not_authorized"

    async with tenant_session(admin) as s:
        await sync_tool_authorizations(
            s, admin_id=admin, instance_id=iid, tools=[_Toggle("schedule_callback", True)]
        )

    async with tenant_session(admin) as s:
        allowed = await ToolBroker(s).dispatch(
            tool,
            {"fire_at": "2026-08-01T10:00Z", "channel": "email", "message": "hi"},
            admin_id=admin,
            instance_id=iid,
        )
        assert allowed.outcome is Outcome.ok


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_disable_is_a_flag_flip_not_a_delete() -> None:
    """Vision §3.7.1: disabling is reversible — the row (and its audit trail) survives."""
    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        await sync_tool_authorizations(
            s, admin_id=admin, instance_id=iid, tools=[_Toggle("send_email", True)]
        )
    async with tenant_session(admin) as s:
        await sync_tool_authorizations(
            s, admin_id=admin, instance_id=iid, tools=[_Toggle("send_email", False)]
        )
    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(InstanceToolAuthorization).where(
                        InstanceToolAuthorization.instance_id == iid
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1  # updated in place, not deleted-and-reinserted
        assert rows[0].enabled is False


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_tool_absent_from_the_payload_keeps_its_authorization() -> None:
    """Full-replace applies to the ids the caller NAMED; silently revoking an unnamed
    tool would be a default-deny surprise for a tool the admin still sees as on."""
    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        await sync_tool_authorizations(
            s,
            admin_id=admin,
            instance_id=iid,
            tools=[_Toggle("send_email", True), _Toggle("send_sms", True)],
        )
    async with tenant_session(admin) as s:
        await sync_tool_authorizations(
            s, admin_id=admin, instance_id=iid, tools=[_Toggle("send_email", False)]
        )
    async with tenant_session(admin) as s:
        state = {
            r.tool_id: r.enabled
            for r in (
                await s.execute(
                    select(InstanceToolAuthorization).where(
                        InstanceToolAuthorization.instance_id == iid
                    )
                )
            ).scalars()
        }
        assert state == {"send_email": False, "send_sms": True}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_authorization_rows_are_tenant_scoped() -> None:
    """Two instances under one admin do not share authorizations."""
    admin = uuid.uuid4()
    a = await _seed(admin)
    async with tenant_session(admin) as s:
        b = uuid.uuid4()
        s.add(
            Instance(
                id=b,
                admin_id=admin,
                name="L2",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
        await sync_tool_authorizations(
            s, admin_id=admin, instance_id=a, tools=[_Toggle("send_email", True)]
        )
    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(InstanceToolAuthorization).where(
                        InstanceToolAuthorization.instance_id == b
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rows == []


# --- the functional tools produce real effects ----------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_schedule_callback_durably_writes_a_deferred_action() -> None:
    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=["schedule_callback"])
    tool = build_tool("schedule_callback")
    assert tool is not None
    async with tenant_session(admin) as s:
        res = await ToolBroker(s).dispatch(
            tool,
            {"fire_at": "2026-08-01T10:00Z", "channel": "sms", "message": "calling back"},
            admin_id=admin,
            instance_id=iid,
        )
        assert res.outcome is Outcome.ok
        assert res.payload["scheduled"] is True

    async with tenant_session(admin) as s:
        rows = (
            (await s.execute(select(DeferredAction).where(DeferredAction.instance_id == iid)))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].action_type == "schedule_callback"
        # §3.3.5: the delivery channel is bound to a connection type at fire time.
        assert rows[0].connection_type == "sms_sender"
        assert rows[0].payload["message"] == "calling back"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_send_email_executes_through_the_platform_sender() -> None:
    admin = uuid.uuid4()
    iid = await _seed(
        admin,
        authorize=["send_email"],
        channels=[{"id": "email", "enabled": True}],
        connections=["email_sender"],
    )
    tool = build_tool("send_email")
    assert tool is not None
    async with tenant_session(admin) as s:
        res = await ToolBroker(s).dispatch(
            tool,
            {"to": "lead@example.com", "body": "Following up."},
            admin_id=admin,
            instance_id=iid,
        )
        # ok = accepted-for-delivery, not inbox-delivered (§3.3.6 honesty).
        assert res.outcome is Outcome.ok
        assert res.payload["state"] == "accepted_for_delivery"
        assert res.payload["providerMessageId"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_send_sms_executes_when_the_sms_channel_is_enabled() -> None:
    admin = uuid.uuid4()
    iid = await _seed(
        admin,
        authorize=["send_sms"],
        channels=[{"id": "sms", "enabled": True}],
        connections=["sms_sender"],
    )
    tool = build_tool("send_sms")
    assert tool is not None
    async with tenant_session(admin) as s:
        res = await ToolBroker(s).dispatch(
            tool, {"to": "+15550001111", "body": "Hi"}, admin_id=admin, instance_id=iid
        )
        assert res.outcome is Outcome.ok
        assert res.payload["providerMessageId"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_send_sms_fails_when_the_tenant_has_designated_no_number() -> None:
    """§3.1.4 BYO: the platform owns no number to fall back on, so a connected Twilio with
    no designated from-number cannot text anyone. Failing is the honest outcome — sending
    from a platform number would put VantageMind in the tenant's telephony path and reach
    the lead from a stranger's number."""
    admin = uuid.uuid4()
    iid = await _seed(
        admin,
        authorize=["send_sms"],
        channels=[{"id": "sms", "enabled": True}],
        connections=[],
    )
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="sms_sender",
                auth_class="api_key",
                provider="twilio",
                status="connected",
                non_secret_config={"accountSid": "AC" + "9" * 30},
            )
        )
        await s.flush()

    tool = build_tool("send_sms")
    assert tool is not None
    async with tenant_session(admin) as s:
        res = await ToolBroker(s).dispatch(
            tool, {"to": "+15550001111", "body": "Hi"}, admin_id=admin, instance_id=iid
        )
        assert res.outcome is Outcome.failed
        assert res.payload == {}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_send_sms_refused_when_the_sms_channel_is_off() -> None:
    """The send_sms↔SMS-channel coupling (§3.3.3 gate 2 / Decision §43): authorized and
    connected is not enough — Luciel must not text on a channel the admin disabled."""
    admin = uuid.uuid4()
    iid = await _seed(
        admin,
        authorize=["send_sms"],
        channels=[{"id": "sms", "enabled": False}],
        connections=["sms_sender"],
    )
    tool = build_tool("send_sms")
    assert tool is not None
    async with tenant_session(admin) as s:
        res = await ToolBroker(s).dispatch(
            tool, {"to": "+15550001111", "body": "Hi"}, admin_id=admin, instance_id=iid
        )
        assert res.outcome is Outcome.failed
        assert res.reason == "channel_not_enabled:sms"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_missing_input_survives_the_real_bindings() -> None:
    """The five-outcome contract is unchanged by the production backends: an absent
    required field is missing_input (a question to ask), not a failure."""
    admin = uuid.uuid4()
    iid = await _seed(
        admin,
        authorize=["send_email"],
        channels=[{"id": "email", "enabled": True}],
        connections=["email_sender"],
    )
    tool = build_tool("send_email")
    assert tool is not None
    async with tenant_session(admin) as s:
        res = await ToolBroker(s).dispatch(
            tool, {"body": "no recipient"}, admin_id=admin, instance_id=iid
        )
        assert res.outcome is Outcome.missing_input
        assert res.missing_fields == ["to"]


# --- connection-gated tools fail cleanly, never fabricate an empty ---------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_connection_gated_tools_refuse_at_gate_3_without_a_connection() -> None:
    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=sorted(CONNECTION_GATED_TOOL_IDS))
    inputs: dict[str, dict[str, object]] = {
        "check_availability": {"window": "next week"},
        "book_appointment": {"slot": "2026-08-01T10:00Z", "email": "a@b.com"},
        "lookup_record": {"query": "order 12"},
        "push_to_crm": {"lead_key": "k", "fields": {"name": "A"}},
        "bring_your_own_webhook": {"payload": {"a": 1}},
    }
    async with tenant_session(admin) as s:
        broker = ToolBroker(s)
        for tool_id in sorted(CONNECTION_GATED_TOOL_IDS):
            tool = build_tool(tool_id)
            assert tool is not None
            res = await broker.dispatch(tool, inputs[tool_id], admin_id=admin, instance_id=iid)
            assert res.outcome is Outcome.failed, tool_id
            assert res.reason.startswith("connection_not_ready:"), tool_id


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_unimplemented_backend_is_failed_never_empty() -> None:
    """§3.3.4 rule 1: with a connection marked connected out of band, the missing OAuth
    backend must still be a structured failure. An ``empty`` here would read to the lead
    as "no slots available" — a false statement about the business's calendar."""
    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=["check_availability"], connections=["calendar"])
    tool = build_tool("check_availability")
    assert tool is not None
    async with tenant_session(admin) as s:
        res = await ToolBroker(s).dispatch(
            tool, {"window": "next week"}, admin_id=admin, instance_id=iid
        )
        assert res.outcome is Outcome.failed
        assert res.outcome is not Outcome.empty
        # The catalog's own except-wrapper names the exception type — diagnostic and
        # non-secret, and unmistakably not a claim about the calendar's contents.
        assert res.reason == "calendar_error: BackendNotConnected"


async def _seed_calendar(admin: uuid.UUID, provider: str, *, with_token: bool = True) -> uuid.UUID:
    from app.connections.secrets import get_secret_store, secret_ref_for

    iid = await _seed(
        admin,
        authorize=["book_appointment"],
        connections=["calendar"],
        connection_providers={"calendar": provider},
    )
    if with_token:
        await get_secret_store().put(
            secret_ref_for(admin, iid, "calendar"), {"access_token": f"tok-{admin}"}
        )
    return iid


def _future_slot() -> str:
    start = (datetime.now(UTC) + timedelta(days=3)).replace(
        hour=10, minute=0, second=0, microsecond=0
    )
    return f"{start.isoformat()}/{(start + timedelta(minutes=30)).isoformat()}"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_google_calendar_connection_gets_the_real_booking_backend() -> None:
    """build_tool takes only a tool_id, so the backend is chosen from the tenant's
    connected calendar PROVIDER at call time. Google resolves to the real backend."""
    from app.tools.calendar_backend import InMemoryCalendarApi

    InMemoryCalendarApi.reset()
    admin = uuid.uuid4()
    iid = await _seed_calendar(admin, "google_calendar")
    tool = build_tool("book_appointment")
    assert tool is not None

    async with tenant_session(admin) as s:
        res = await ToolBroker(s).dispatch(
            tool,
            {"slot": _future_slot(), "email": "lead@example.com"},
            admin_id=admin,
            instance_id=iid,
        )
    assert res.outcome is Outcome.ok, res.reason
    assert res.payload["bookingRef"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_calendly_stays_honestly_unavailable() -> None:
    """No Calendly OAuth client is registered yet, so a Calendly calendar must keep
    failing cleanly rather than borrowing the Google backend and booking nowhere."""
    admin = uuid.uuid4()
    iid = await _seed_calendar(admin, "calendly")
    tool = build_tool("book_appointment")
    assert tool is not None

    async with tenant_session(admin) as s:
        res = await ToolBroker(s).dispatch(
            tool,
            {"slot": _future_slot(), "email": "lead@example.com"},
            admin_id=admin,
            instance_id=iid,
        )
    assert res.outcome is Outcome.failed
    assert res.reason == "calendar_error: BackendNotConnected"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_google_calendar_without_a_stored_token_is_failed_never_empty() -> None:
    from app.tools.calendar_backend import InMemoryCalendarApi

    InMemoryCalendarApi.reset()
    admin = uuid.uuid4()
    iid = await _seed_calendar(admin, "google_calendar", with_token=False)
    tool = build_tool("book_appointment")
    assert tool is not None

    async with tenant_session(admin) as s:
        res = await ToolBroker(s).dispatch(
            tool,
            {"slot": _future_slot(), "email": "lead@example.com"},
            admin_id=admin,
            instance_id=iid,
        )
    assert res.outcome is Outcome.failed
    assert res.reason == "calendar_error: CalendarUnavailable"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_every_dispatch_outcome_is_traced() -> None:
    """§3.3.4 rule 3 — including the refusals, which is where a silent drop would hide."""
    admin = uuid.uuid4()
    iid = await _seed(
        admin,
        authorize=["send_email"],
        channels=[{"id": "email", "enabled": True}],
        connections=["email_sender"],
    )
    tool = build_tool("send_email")
    assert tool is not None
    async with tenant_session(admin) as s:
        broker = ToolBroker(s)
        await broker.dispatch(tool, {"to": "a@b.com", "body": "x"}, admin_id=admin, instance_id=iid)
        await broker.dispatch(tool, {"body": "x"}, admin_id=admin, instance_id=iid)
        assert [t["tool"] for t in broker.traces] == ["send_email", "send_email"]
        assert {t["outcome"] for t in broker.traces} == {"ok", "missing_input"}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_byo_webhook_attaches_the_stored_auth_header(monkeypatch) -> None:
    """§3.8.6: the admin's registered Authorization header is read from the
    SecretStore by the PARENT process and attached at dispatch. It was hardcoded
    to None — every authenticated endpoint 401'd while the tool looked fine."""
    from app.connections import sandbox
    from app.connections.secrets import get_secret_store
    from app.tools import contract as c

    admin = uuid.uuid4()
    iid = await _seed(admin, authorize=["bring_your_own_webhook"])
    secret_ref = f"vm/{admin}/{iid}/outbound_webhook"
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="outbound_webhook",
                auth_class="long_lived_token",
                provider="custom_webhook",
                status="connected",
                secret_ref=secret_ref,
                non_secret_config={
                    "url": "https://hooks.example.com/leads",
                    "egressAllowlist": ["hooks.example.com"],
                },
            )
        )
    await get_secret_store().put(secret_ref, {"auth_header": "Bearer tenant-token"})

    seen: dict[str, object] = {}

    async def _capture(**kwargs):  # noqa: ANN003 — mirror run_byo_webhook's kwargs
        seen.update(kwargs)
        return c.ok({"status": 200})

    monkeypatch.setattr(sandbox, "run_byo_webhook", _capture)

    tool = build_tool("bring_your_own_webhook")
    assert tool is not None
    async with tenant_session(admin) as s:
        broker = ToolBroker(s)
        res = await broker.dispatch(
            tool, {"payload": {"a": 1}}, admin_id=admin, instance_id=iid
        )
    assert res.outcome is Outcome.ok
    assert seen["auth_header"] == "Bearer tenant-token"
    assert seen["url"] == "https://hooks.example.com/leads"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_successful_booking_writes_the_durable_booking_event() -> None:
    """§3.9: the analytics count is fed by the SAME dispatch that told the
    customer "booked" — an ok write, one row; a failed/conflict write, none."""
    from app.models.booking import BookingEvent
    from app.tools.calendar_backend import InMemoryCalendarApi

    InMemoryCalendarApi.reset()
    admin = uuid.uuid4()
    iid = await _seed_calendar(admin, "google_calendar")
    tool = build_tool("book_appointment")
    assert tool is not None

    async with tenant_session(admin) as s:
        res = await ToolBroker(s).dispatch(
            tool,
            {"slot": _future_slot(), "email": "lead@example.com"},
            admin_id=admin,
            instance_id=iid,
        )
        assert res.outcome is Outcome.ok, res.reason

    async with tenant_session(admin) as s:
        events = (
            (await s.execute(select(BookingEvent).where(BookingEvent.admin_id == admin)))
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].booking_ref == res.payload["bookingRef"]
