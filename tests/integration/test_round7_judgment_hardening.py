"""Luciel's runtime judgment, hardened (round 7, review:judgment findings).

Every test here drives the Orchestrator through real turns, the way the established
loop tests do — a deterministic model double, a fake retriever, the tenant session —
and asserts on what the customer read, what the owner was paged, and what the rows
say. Nothing is mocked below the loop except the outbound senders.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from app.db.session import tenant_session
from app.models.conversation import EscalationEvent
from app.models.conversation import Session as SessionModel
from app.models.tool_auth import InstanceToolAuthorization
from app.notifications import escalation_dispatch
from app.runtime.escalation import Signal
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import (
    _HANDOFF_ACK_MARK,
    ASK_ATTEMPT_BOUND,
    DECLINED_INPUT_LINE,
    SHORT_HANDOFF_ACKS,
    Orchestrator,
    handoff_ack_sent,
)
from app.runtime.session_close import finalize_session
from app.runtime.testing import EchoModel
from app.tools import contract as tool_contract
from app.tools.contract import ToolResult
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

UNCOVERED = "do you also do crown lengthening?"
GUESS = "Yes — crown lengthening is $650 and takes about an hour."


async def _unrelated_retriever(_q: str) -> list[str]:
    # Shares nothing with the guess: the draft lands below the floor.
    return ["Our showroom is open Saturdays from 9 to 3 and closed on Sundays."]


async def _seed_session(s, admin, iid, *, channel: str = "widget") -> uuid.UUID:
    sid = uuid.uuid4()
    s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel=channel, mode="ai"))
    await s.flush()
    return sid


def _orch(s, admin, iid, *, primary=None, retriever=_unrelated_retriever, **kw) -> Orchestrator:
    return Orchestrator(
        session=s,
        router=LLMRouter(
            primary=primary or EchoModel(confidence=1.0, fixed_reply=GUESS), fallback=EchoModel()
        ),
        retriever=retriever,
        admin_id=admin,
        instance_id=iid,
        enabled_channels={"widget", "sms", "email"},
        has_payment_method=False,
        **kw,
    )


async def _turn(orch: Orchestrator, sid: uuid.UUID, text: str, *, channel: str = "widget", **kw):
    return await orch.run_turn(
        session_id=sid,
        resolved_lead_id=None,
        channel=channel,
        mode=kw.pop("mode", "ai"),
        inbound_text=text,
        received_at=datetime.now(UTC),
        **kw,
    )


async def _rows(s, sid: uuid.UUID) -> list[EscalationEvent]:
    return list(
        (
            await s.execute(
                select(EscalationEvent)
                .where(EscalationEvent.session_id == sid)
                .order_by(EscalationEvent.created_at)
            )
        )
        .scalars()
        .all()
    )


async def _session_row(s, sid: uuid.UUID) -> SessionModel:
    return (await s.execute(select(SessionModel).where(SessionModel.id == sid))).scalar_one()


# --- finding 2: a take-over always gets the take-over wording ----------------------- #
@pytest.mark.parametrize(
    ("second", "reasons_head"),
    [
        ("ok can I just talk to a real person please", None),
        ("I will kill you if you charge me again", "moderation:threats_violence"),
    ],
)
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_take_over_after_an_earlier_handoff_ack_gets_the_full_line_not_the_short_one(
    second: str, reasons_head: str | None
):
    """Turn 1 is an uncovered question: the full hand-off ack goes out and the session
    now "has" it. Turn 2 asks for a person (or threatens): the conversation is handed
    to a person for good. The reply used to be the short "That's another one I can't
    answer from what I have" cycle meant for a LATER question Luciel keeps answering
    around — read as not understanding, right before every later message got silence."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        first = await _turn(_orch(s, admin, iid), sid, UNCOVERED)
        assert first.escalation is not None and first.escalation.answer_ungrounded
        assert _HANDOFF_ACK_MARK.search(first.reply_text)  # the full ack went out
        assert handoff_ack_sent(await _history_of(s, admin, sid))

        taken = await _turn(_orch(s, admin, iid), sid, second)
        assert taken.handed_off is True
        assert taken.escalation is not None
        assert taken.escalation.signal is Signal.explicit_human_request
        # The take-over wording, never the repeat-ack line.
        assert not any(taken.reply_text.startswith(ack) for ack in SHORT_HANDOFF_ACKS)
        assert _HANDOFF_ACK_MARK.search(taken.reply_text)
        assert "follow up with you" in taken.reply_text
        assert (await _session_row(s, sid)).mode == "human_controlled"
        rows = await _rows(s, sid)
        assert sorted(r.signal for r in rows) == ["cannot_answer", "explicit_human_request"]
        if reasons_head is not None:
            taken_row = next(r for r in rows if r.signal == "explicit_human_request")
            assert list(taken_row.reasons or [])[0] == reasons_head


async def _history_of(s, admin: uuid.UUID, sid: uuid.UUID):
    from app.runtime import memory

    return await memory.load_session_history(s, admin_id=admin, session_id=sid)


# --- shared: tools with scripted results, a counting model, the owner's pages ------- #
class _ScriptedTool:
    """A tool whose results are scripted per call (no connection, no channels)."""

    requires_connection: str | None = None
    requires_channels: frozenset[str] = frozenset()
    execution_mode = "in_process"
    display_name = "Scripted"
    description = "scripted"

    def __init__(
        self, tool_id: str, required_inputs: frozenset[str], results: list[ToolResult]
    ) -> None:
        self.tool_id = tool_id
        self.required_inputs = required_inputs
        self._results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def execute(self, inputs: dict[str, Any], context: Any) -> ToolResult:
        self.calls.append(dict(inputs))
        return self._results.pop(0)


class _CountingEcho(EchoModel):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.calls = 0

    async def complete(self, system_prompt, messages, *, tools=None):  # type: ignore[override]
        self.calls += 1
        return await super().complete(system_prompt, messages, tools=tools)


@pytest.fixture
def paged(monkeypatch) -> list[str]:
    """The signal of every owner page scheduled in this test; each lands once the
    turn's transaction commits and the background task is drained."""
    signals: list[str] = []

    async def fake_notify(session, **kw):
        signals.append(str(kw["signal"]))

    monkeypatch.setattr(escalation_dispatch, "notify_escalation_safely", fake_notify)
    return signals


async def _authorize(s, admin: uuid.UUID, iid: uuid.UUID, tool: _ScriptedTool) -> None:
    s.add(
        InstanceToolAuthorization(
            admin_id=admin, instance_id=iid, tool_id=tool.tool_id, enabled=True
        )
    )
    await s.flush()


def _tool_orch(
    s, admin, iid, *, primary, tool: _ScriptedTool, retriever=_unrelated_retriever
) -> Orchestrator:
    return _orch(
        s,
        admin,
        iid,
        primary=primary,
        retriever=retriever,
        tool_resolver=lambda tid: tool if tid == tool.tool_id else None,
    )


def _reasons(rows: list[EscalationEvent]) -> list[str]:
    return [str(r) for row in rows for r in (row.reasons or [])]


# --- finding 5: the ask counter means consecutive stalls ---------------------------- #
PRICE_CHUNK = "A standard cleaning is $120 and takes about forty-five minutes."
EMAIL_ASK = "What email should I send the confirmation to?"
SLOT = "2026-10-01T14:00:00"


async def _price_retriever(_q: str) -> list[str]:
    return [PRICE_CHUNK]


def _booking_model() -> _CountingEcho:
    """Proposes the booking with the slot alone, then asks for the email it lacks."""
    return _CountingEcho(
        confidence=1.0,
        tool_call_script=[[{"tool_id": "book_appointment", "inputs": {"slot": SLOT}}]],
        fixed_reply=EMAIL_ASK,
    )


async def _booking_stall(s, admin, iid, sid, tool: _ScriptedTool, text: str):
    result = await _turn(_tool_orch(s, admin, iid, primary=_booking_model(), tool=tool), sid, text)
    assert result.tool_outcomes == ["missing_input"]
    return result


async def _price_question(s, admin, iid, sid, tool: _ScriptedTool):
    model = _CountingEcho(confidence=1.0, fixed_reply=PRICE_CHUNK)
    result = await _turn(
        _tool_orch(s, admin, iid, primary=model, tool=tool, retriever=_price_retriever),
        sid,
        "how much is a cleaning?",
    )
    assert result.tool_outcomes == [] and PRICE_CHUNK in result.reply_text
    return result


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_ordinary_no_after_the_gather_moved_on_is_not_a_decline(paged: list[str]):
    """T1 stalls a booking for want of an email (the counter reads 1). T2 is a price
    question answered from the knowledge — no tool, no stall — but the counter used
    to stay at 1 for the rest of the session. T3 "No thanks" (to "anything else?") was
    then read as refusing the email: the decline path appended "customer declined to
    give email" to the session's cannot_answer row, paged the owner again, told the
    customer it was passed to the team, and called no model. The counter now means
    stalls IN A ROW: T2 clears it, T3 is ordinary conversation, and the close finds no
    booking still stalled."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    tool = _ScriptedTool("book_appointment", frozenset({"slot", "email"}), [])
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        await _authorize(s, admin, iid, tool)
        await _booking_stall(s, admin, iid, sid, tool, "book me a cleaning for 2pm on the 1st")
        assert (await _session_row(s, sid)).input_ask_attempts == 1

        await _price_question(s, admin, iid, sid, tool)
        assert (await _session_row(s, sid)).input_ask_attempts == 0  # the gather moved on

        model = _CountingEcho(confidence=1.0, fixed_reply="No problem at all, have a great day!")
        third = await _turn(_tool_orch(s, admin, iid, primary=model, tool=tool), sid, "No thanks")
        assert model.calls == 1  # answered by the model, never by the decline path
        assert DECLINED_INPUT_LINE not in third.reply_text
        assert third.escalation is None
        assert not any("customer declined" in r for r in _reasons(await _rows(s, sid)))
        assert (await _session_row(s, sid)).input_ask_attempts == 0
    await escalation_dispatch.drain_background_notifications()
    pages_before_close = list(paged)

    # The session ends: with no gather pending, no "booking stalled" row and no page.
    async with tenant_session(admin) as s:
        assert await finalize_session(
            s, admin_id=admin, instance_id=iid, channel="widget", session_id=sid
        )
        assert not any("conversation ended" in r for r in _reasons(await _rows(s, sid)))
    await escalation_dispatch.drain_background_notifications()
    assert paged == pages_before_close


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_ask_bound_counts_consecutive_stalls_only():
    """§3.3.7: the bound is ASK_ATTEMPT_BOUND stalled turns IN A ROW. A stall, an
    ordinary answered turn, then a stall used to read 2 — "still missing email after 2
    asks", the hand-off line instead of what was only the first re-ask."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    tool = _ScriptedTool("book_appointment", frozenset({"slot", "email"}), [])
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        await _authorize(s, admin, iid, tool)
        await _booking_stall(s, admin, iid, sid, tool, "book me a cleaning for 2pm on the 1st")
        await _price_question(s, admin, iid, sid, tool)
        again = await _booking_stall(s, admin, iid, sid, tool, "ok, just book the 2pm")
        assert ASK_ATTEMPT_BOUND == 2
        assert (await _session_row(s, sid)).input_ask_attempts == 1  # the first in a row
        assert again.handed_off is False
        assert EMAIL_ASK in again.reply_text  # the ask goes out; no bound hand-off
        assert not any("after 2 asks" in r for r in _reasons(await _rows(s, sid)))


WHICH_ONE = "Which one should I cancel — Tuesday 10am or Thursday 3pm?"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_choice_framed_as_a_drop_answers_the_ambiguity_instead_of_declining_it():
    """A cancel came back ambiguous (two bookings) and Luciel asked which one. "drop
    the Tuesday one" names the choice; it used to match the decline classifier's
    "drop the …" and be treated as refusing to say which — a cannot_answer row
    "customer declined to say which one they meant", the owner paged, the customer
    told the cancellation was passed to the team, and no model call."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    tool = _ScriptedTool(
        "cancel_appointment",
        frozenset({"email"}),
        [
            tool_contract.ambiguous(
                [{"bookingRef": "tue-1", "start": "Tuesday 10am"}, {"bookingRef": "thu-1"}]
            ),
            tool_contract.ok({"cancelled": True, "bookingRef": "tue-1"}),
        ],
    )
    email = "dana@lead.example"
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        await _authorize(s, admin, iid, tool)
        asked = await _turn(
            _tool_orch(
                s,
                admin,
                iid,
                primary=_CountingEcho(
                    confidence=1.0,
                    tool_call_script=[
                        [{"tool_id": "cancel_appointment", "inputs": {"email": email}}]
                    ],
                    fixed_reply=WHICH_ONE,
                ),
                tool=tool,
            ),
            sid,
            f"cancel my appointment, it's under {email}",
        )
        assert asked.tool_outcomes == ["ambiguous"] and WHICH_ONE in asked.reply_text
        assert (await _session_row(s, sid)).input_ask_attempts == 1

        model = _CountingEcho(
            confidence=1.0,
            tool_call_script=[
                [
                    {
                        "tool_id": "cancel_appointment",
                        "inputs": {"email": email, "booking_ref": "tue-1"},
                    }
                ]
            ],
            fixed_reply="Done — the Tuesday 10am booking is cancelled.",
        )
        chosen = await _turn(
            _tool_orch(s, admin, iid, primary=model, tool=tool), sid, "drop the Tuesday one"
        )
        assert model.calls == 2  # the choice reached the model and the tool ran
        assert chosen.tool_outcomes == ["ok"]
        assert tool.calls[-1]["booking_ref"] == "tue-1"
        assert DECLINED_INPUT_LINE not in chosen.reply_text
        assert "cancelled" in chosen.reply_text
        assert not any("declined to say which" in r for r in _reasons(await _rows(s, sid)))
        assert (await _session_row(s, sid)).input_ask_attempts == 0


# --- finding 3: the lead record is read back only to the person it proves ------------ #
JANE_PHONE = "+15551234567"
JANE_EMAIL = "jane@example.com"
STARTER = "The starter package includes onboarding, two channels and email support."


async def _starter_retriever(_q: str) -> list[str]:
    return [STARTER]


class _BookingTool(_ScriptedTool):
    """book_appointment that confirms with whatever it was given — the readback a
    customer sees names the booked email."""

    def __init__(self) -> None:
        super().__init__("book_appointment", frozenset({"slot", "email"}), [])

    async def execute(self, inputs: dict[str, Any], context: Any) -> ToolResult:
        self.calls.append(dict(inputs))
        return tool_contract.ok(
            {"bookingRef": "bk-1", "start": inputs["slot"], "email": inputs["email"]}
        )


@pytest.fixture
def delivered(monkeypatch) -> list[dict[str, object]]:
    """Every channel-switch send (to, channel); nothing leaves the test."""
    from app.channels import outbound
    from app.channels.outbound import OutboundResult

    calls: list[dict[str, object]] = []

    async def fake(session, *, admin_id, instance_id, channel, to, body, subject=None, **kw):
        calls.append({"channel": channel, "to": to})
        return OutboundResult(channel=channel, delivered=True, detail=None)

    monkeypatch.setattr(outbound, "send_outbound_reply", fake)
    return calls


async def _jane_on_file(s, admin: uuid.UUID, iid: uuid.UUID) -> uuid.UUID:
    """Jane texted once (her number confirmed by the transport) and confirmed her
    email in that conversation: an existing customer with both on her record."""
    from app.models.lead import Lead
    from app.runtime import channel_switch

    sms_sid = await _seed_session(s, admin, iid, channel="sms")
    opener = EchoModel(confidence=1.0, fixed_reply="Happy to help — when suits you?")
    await _orch(s, admin, iid, primary=opener).run_turn(
        session_id=sms_sid,
        resolved_lead_id=None,
        channel="sms",
        mode="ai",
        inbound_text="hi, can I book a cleaning?",
        transport_identifier=JANE_PHONE,
        received_at=datetime.now(UTC),
    )
    lead = (
        await s.execute(select(Lead).where(Lead.admin_id == admin, Lead.instance_id == iid))
    ).scalar_one()
    lead.email = JANE_EMAIL
    await channel_switch.confirm_identifier(
        s,
        admin_id=admin,
        instance_id=iid,
        lead_id=lead.id,
        kind="email",
        value=JANE_EMAIL,
        source="volunteered",
    )
    await s.flush()
    return lead.id


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_widget_visitor_typing_a_customers_number_is_never_given_her_email(
    delivered: list[dict[str, object]],
):
    """Jane is an existing SMS customer with a confirmed email. A widget visitor types
    Jane's number and asks to book: the session binds to Jane's record (a typed
    identifier is all the widget has). The broker used to fill the booking's email
    from Jane's record and the readback ended "To confirm the exact details: … email:
    jane@…" — one customer's address handed to whoever knew her number, and a booking
    (or a cancel) made under it. On the widget the record now fills only what the
    session itself supplied: the email is ASKED for. "Email me the details" neither
    sends to Jane's address nor offers it back."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    tool = _BookingTool()
    async with tenant_session(admin) as s:
        jane = await _jane_on_file(s, admin, iid)
        await _authorize(s, admin, iid, tool)
        sid = await _seed_session(s, admin, iid, channel="widget")

        asked = await _turn(
            _tool_orch(s, admin, iid, primary=_booking_model(), tool=tool),
            sid,
            f"it's {JANE_PHONE}, book me a cleaning for 2pm on the 1st",
        )
        assert (await _session_row(s, sid)).resolved_lead_id == jane  # bound by the number
        assert asked.tool_outcomes == ["missing_input"]
        assert tool.calls == []  # nothing booked under Jane's email
        assert JANE_EMAIL not in asked.reply_text
        assert EMAIL_ASK in asked.reply_text

        details = await _turn(
            _tool_orch(
                s,
                admin,
                iid,
                primary=_CountingEcho(confidence=1.0, fixed_reply=STARTER),
                tool=tool,
                retriever=_starter_retriever,
            ),
            sid,
            "can you email me the details of the starter package?",
        )
        assert delivered == []  # never sent to the record's address
        assert JANE_EMAIL not in details.reply_text  # nor offered back as a question
        assert "What's the best email address to send it to?" in details.reply_text


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_widget_visitor_who_types_the_email_themselves_gets_it_filled():
    """The same record, but THIS session supplied the address ("my email is …"): it
    may fill the booking and be read back — the visitor gave it."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    tool = _BookingTool()
    async with tenant_session(admin) as s:
        await _jane_on_file(s, admin, iid)
        await _authorize(s, admin, iid, tool)
        sid = await _seed_session(s, admin, iid, channel="widget")
        model = _CountingEcho(
            confidence=1.0,
            tool_call_script=[[{"tool_id": "book_appointment", "inputs": {"slot": SLOT}}]],
            fixed_reply="Booked — see you then.",
        )
        booked = await _turn(
            _tool_orch(s, admin, iid, primary=model, tool=tool),
            sid,
            f"my email is {JANE_EMAIL}, book me a cleaning for 2pm on the 1st",
        )
        assert booked.tool_outcomes == ["ok"]
        assert tool.calls == [{"slot": SLOT, "email": JANE_EMAIL}]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_on_sms_the_transport_number_fills_the_phone_and_the_record_fills_the_email():
    """A strong channel proves who is writing: the record's confirmed email fills, and
    the phone is the number the message came from, not some other number on file."""
    from app.runtime import channel_switch

    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        jane = await _jane_on_file(s, admin, iid)
        await channel_switch.confirm_identifier(
            s,
            admin_id=admin,
            instance_id=iid,
            lead_id=jane,
            kind="phone",
            value="+15559876543",
            source="volunteered",
        )
        on_sms = await channel_switch.confirmed_inputs_on_file(
            s,
            admin_id=admin,
            instance_id=iid,
            lead_id=jane,
            transport_identifier=JANE_PHONE,
            channel="sms",
        )
        assert on_sms == {"email": JANE_EMAIL, "phone": JANE_PHONE}
        on_widget = await channel_switch.confirmed_inputs_on_file(
            s, admin_id=admin, instance_id=iid, lead_id=jane, channel="widget"
        )
        assert on_widget == {}
        typed = await channel_switch.confirmed_inputs_on_file(
            s,
            admin_id=admin,
            instance_id=iid,
            lead_id=jane,
            channel="widget",
            supplied=frozenset({JANE_EMAIL}),
        )
        assert typed == {"email": JANE_EMAIL}


# --- finding 4: the close's stalled-booking row is written AND its page scheduled ---- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_sweep_close_of_a_stalled_sms_booking_schedules_one_page_naming_the_field(
    paged: list[str],
):
    """An SMS customer's booking stalls for want of an email and they go quiet. The
    hourly sweep seals the session: the cannot_answer row names the missing field and
    the conversation's end, and the owner page is scheduled on the sweep's own commit
    (the process that runs the sweep must drain it before exiting — app/jobs)."""
    from datetime import timedelta

    from app.runtime.session_close import finalize_due_sessions

    admin, iid = uuid.uuid4(), uuid.uuid4()
    tool = _ScriptedTool("book_appointment", frozenset({"slot", "email"}), [])
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid, channel="sms")
        await _authorize(s, admin, iid, tool)
        stalled = await _turn(
            _tool_orch(s, admin, iid, primary=_booking_model(), tool=tool),
            sid,
            "book me a cleaning for 2pm on the 1st",
            channel="sms",
            transport_identifier=JANE_PHONE,
        )
        assert stalled.tool_outcomes == ["missing_input"]
        assert (await _session_row(s, sid)).input_ask_attempts == 1
    await escalation_dispatch.drain_background_notifications()
    before = list(paged)

    async with tenant_session(admin) as s:
        later = datetime.now(UTC) + timedelta(hours=9)
        assert await finalize_due_sessions(s, admin_id=admin, now=later) == 1
        assert paged == before  # nothing starts before the sweep's transaction commits
    await escalation_dispatch.drain_background_notifications()
    assert paged[len(before) :] == ["cannot_answer"]  # one page for the stalled booking
    async with tenant_session(admin) as s:
        (row,) = [r for r in await _rows(s, sid) if r.signal == "cannot_answer"]
        assert "still missing email" in list(row.reasons or [])
        assert "the conversation ended before the booking could be completed" in list(
            row.reasons or []
        )


# --- finding 5 (cont.): a stall for a DIFFERENT detail is a new ask, not a re-ask --- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_gather_that_progresses_from_the_time_to_the_email_is_not_a_repeated_ask(
    paged: list[str],
):
    """T1 "book me a cleaning": the broker refuses for want of a slot and an email and
    Luciel asks when. T2 the customer gives the time; the broker now waits only for
    the email. That is progress — the customer answered — but the counter read 2 and
    the bound fired: the hand-off line instead of the email ask, and a cannot_answer
    row "still missing email after 2 asks" although the email was asked for once.
    The pending set changed, so T2 is the first ask for it; only a third turn that
    still lacks the email after it was asked reaches the bound. Each turn is its own
    transaction, as every inbound message is live."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    tool = _ScriptedTool("book_appointment", frozenset({"slot", "email"}), [])
    when_ask = "What day and time would suit you?"
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        await _authorize(s, admin, iid, tool)
        first = await _turn(
            _tool_orch(
                s,
                admin,
                iid,
                primary=_CountingEcho(
                    confidence=1.0,
                    tool_call_script=[[{"tool_id": "book_appointment", "inputs": {}}]],
                    fixed_reply=when_ask,
                ),
                tool=tool,
            ),
            sid,
            "book me a cleaning",
        )
        assert first.tool_outcomes == ["missing_input"]
        assert (await _session_row(s, sid)).input_ask_attempts == 1

    async with tenant_session(admin) as s:
        second = await _turn(
            _tool_orch(s, admin, iid, primary=_booking_model(), tool=tool), sid, "2pm on the 1st"
        )
        assert second.tool_outcomes == ["missing_input"]
        assert (await _session_row(s, sid)).input_ask_attempts == 1  # a new ask
        assert second.handed_off is False
        assert EMAIL_ASK in second.reply_text  # the email is asked for, once
        assert not any("after 2 asks" in r for r in _reasons(await _rows(s, sid)))

    # The same detail still missing after it was asked: now it is a re-ask.
    async with tenant_session(admin) as s:
        third = await _turn(
            _tool_orch(s, admin, iid, primary=_booking_model(), tool=tool),
            sid,
            "just book it please",
        )
        assert EMAIL_ASK not in third.reply_text  # the bound: no third ask
        assert "still missing email after 2 asks" in _reasons(await _rows(s, sid))
        assert (await _session_row(s, sid)).input_ask_attempts == 0
    await escalation_dispatch.drain_background_notifications()


# --- finding 6: a mentioned address is a destination, never the lead's own ---------- #
KAREN = "karen@books.com"


async def _confirmed_values(s, admin: uuid.UUID) -> list[str]:
    from app.models.lead import LeadIdentifier

    return [
        str(v)
        for v in (
            await s.execute(
                select(LeadIdentifier.value).where(
                    LeadIdentifier.admin_id == admin,
                    LeadIdentifier.kind == "email",
                    LeadIdentifier.confirmed_at.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    ]


def _answer_orch(s, admin, iid, *, tool: _ScriptedTool | None = None) -> Orchestrator:
    model = _CountingEcho(confidence=1.0, fixed_reply=STARTER)
    if tool is None:
        return _orch(s, admin, iid, primary=model, retriever=_starter_retriever)
    return _tool_orch(s, admin, iid, primary=model, tool=tool, retriever=_starter_retriever)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_accountants_address_in_an_email_me_request_is_sent_to_but_never_confirmed(
    delivered: list[dict[str, object]],
):
    """An SMS customer: "email the details … to my accountant at karen@…". The answer
    goes to Karen, as asked. It used to ALSO be confirmed as the customer's own email
    (lead capture had classified it as a mention): from then on a booking was filled
    with Karen's address and read back, and a later "email me the details" went to
    Karen without asking."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    tool = _BookingTool()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid, channel="sms")
        await _authorize(s, admin, iid, tool)
        sent = await _turn(
            _answer_orch(s, admin, iid, tool=tool),
            sid,
            f"can you email the details of the starter package to my accountant at {KAREN}",
            channel="sms",
            transport_identifier=JANE_PHONE,
        )
        assert delivered == [{"channel": "email", "to": KAREN}]  # this one send: as asked
        assert f"I've also emailed this to {KAREN}." in sent.reply_text
        assert KAREN not in await _confirmed_values(s, admin)  # never the lead's own

        booked = await _turn(
            _tool_orch(s, admin, iid, primary=_booking_model(), tool=tool),
            sid,
            "book me a cleaning for 2pm on the 1st",
            channel="sms",
            transport_identifier=JANE_PHONE,
        )
        assert booked.tool_outcomes == ["missing_input"]  # Karen's address fills nothing
        assert tool.calls == []

        again = await _turn(
            _answer_orch(s, admin, iid, tool=tool),
            sid,
            "can you email me the details of the starter package?",
            channel="sms",
            transport_identifier=JANE_PHONE,
        )
        assert delivered == [{"channel": "email", "to": KAREN}]  # nothing more to Karen
        assert KAREN not in again.reply_text
        assert "What's the best email address to send it to?" in again.reply_text


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_spouses_address_given_as_the_destination_is_sent_once_and_not_kept(
    delivered: list[dict[str, object]],
):
    """The parked path: asked where to send it, the customer names their wife's
    address. It is that one send's destination — sent, said truthfully ("Sent to …",
    not "check your email") — and not confirmed onto the lead; a bare address typed in
    answer is still the customer's own and is kept."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    wife = "maria@home.example"
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid, channel="sms")
        asked = await _turn(
            _answer_orch(s, admin, iid),
            sid,
            "can you email me the details of the starter package?",
            channel="sms",
            transport_identifier=JANE_PHONE,
        )
        assert "What's the best email address to send it to?" in asked.reply_text

        settled = await _turn(
            _answer_orch(s, admin, iid),
            sid,
            f"send it to my wife at {wife}",
            channel="sms",
            transport_identifier=JANE_PHONE,
        )
        assert delivered == [{"channel": "email", "to": wife}]
        assert settled.reply_text == f"Sent to {wife}."
        assert await _confirmed_values(s, admin) == []

        await _turn(
            _answer_orch(s, admin, iid),
            sid,
            "can you email me the details of the starter package?",
            channel="sms",
            transport_identifier=JANE_PHONE,
        )
        own = await _turn(
            _answer_orch(s, admin, iid),
            sid,
            "dana@lead.example",
            channel="sms",
            transport_identifier=JANE_PHONE,
        )
        assert own.reply_text == "Sent — check your email at dana@lead.example."
        assert await _confirmed_values(s, admin) == ["dana@lead.example"]


# --- finding 7: every take-over pages, and only a real page is claimed ---------------- #
async def _take_over_then_hand_back(admin: uuid.UUID, iid: uuid.UUID, sid: uuid.UUID) -> None:
    """T1 asks for a person on SMS (row + page + take-over, committed); the owner
    replies from the dashboard and hands the conversation back (its own commit)."""
    from app.runtime import handoff

    async with tenant_session(admin) as s:
        first = await _turn(
            _orch(s, admin, iid),
            sid,
            "can I talk to a real person please",
            channel="sms",
            transport_identifier=JANE_PHONE,
        )
        assert first.handed_off is True
        assert (await _session_row(s, sid)).mode == "human_controlled"
    await escalation_dispatch.drain_background_notifications()
    async with tenant_session(admin) as s:
        await handoff.hand_back(
            s, admin_id=admin, instance_id=iid, session_id=sid, actor_user_id=None
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_second_request_for_a_person_after_a_hand_back_pages_the_owner_again(
    paged: list[str],
):
    """The owner took the first request, replied and handed back. Two hours later, same
    SMS thread: "can I speak to someone again?". The session's explicit_human_request
    row already existed, the insert met it and did nothing — Luciel said it had passed
    this to the team, the session went to a person, and nobody was paged: every later
    message went unanswered. Each take-over now re-pages the row."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid, channel="sms")
    await _take_over_then_hand_back(admin, iid, sid)
    assert paged == ["explicit_human_request"]

    async with tenant_session(admin) as s:
        again = await _turn(
            _orch(s, admin, iid),
            sid,
            "can I speak to someone again please",
            channel="sms",
            transport_identifier=JANE_PHONE,
        )
        assert again.handed_off is True
        assert _HANDOFF_ACK_MARK.search(again.reply_text)
        assert (await _session_row(s, sid)).mode == "human_controlled"
        assert paged == ["explicit_human_request"]  # nothing starts before the commit
    await escalation_dispatch.drain_background_notifications()
    assert paged == ["explicit_human_request", "explicit_human_request"]
    async with tenant_session(admin) as s:
        (row,) = await _rows(s, sid)  # still one business event: the session's row
        assert row.signal == "explicit_human_request"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_self_harm_disclosure_after_a_hand_back_is_a_real_safety_page():
    """Same session, the second take-over is a self-harm disclosure. SELF_HARM_REPLY
    says "I've also let the team here know" — and it used to be untrue: no row was
    written and no safety page went out. Now the row leads with the safety category
    (the dispatcher routes and labels the page on its FIRST reason) and the page is
    scheduled, so the words are true."""
    from app.notifications.base import safety_category_from_reasons
    from app.runtime.orchestrator import SELF_HARM_REPLY

    admin, iid = uuid.uuid4(), uuid.uuid4()
    pages: list[dict[str, Any]] = []

    async def fake_notify(session, **kw):
        pages.append(kw)

    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid, channel="sms")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(escalation_dispatch, "notify_escalation_safely", fake_notify)
        await _take_over_then_hand_back(admin, iid, sid)
        assert len(pages) == 1

        async with tenant_session(admin) as s:
            disclosed = await _turn(
                _orch(s, admin, iid),
                sid,
                "honestly I want to kill myself",
                channel="sms",
                transport_identifier=JANE_PHONE,
            )
            assert disclosed.reply_text == SELF_HARM_REPLY
            assert disclosed.handed_off is True
        await escalation_dispatch.drain_background_notifications()
    assert len(pages) == 2 and pages[1]["signal"] == "explicit_human_request"
    async with tenant_session(admin) as s:
        (row,) = await _rows(s, sid)
        reasons = tuple(str(r) for r in (row.reasons or []))
        assert reasons[0] == "moderation:self_harm"
        assert safety_category_from_reasons(reasons) == "self_harm"
        assert "asked for a person" in reasons  # the first take-over's reason stays


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_take_over_that_could_not_be_recorded_never_claims_the_team_was_told(
    monkeypatch, paged: list[str]
):
    """When the take-over's row + page could not be written, nothing may say the team
    knows and the conversation must not be handed to a person nobody paged: the
    self-harm disclosure still gets the crisis lines, without the claim, and Luciel
    keeps the conversation."""
    from app.runtime import orchestrator as orch_mod

    async def not_recorded(*_a: Any, **_kw: Any) -> bool:
        return False

    monkeypatch.setattr(orch_mod, "record_takeover_escalation", not_recorded)
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid, channel="sms")
        result = await _turn(
            _orch(s, admin, iid),
            sid,
            "honestly I want to kill myself",
            channel="sms",
            transport_identifier=JANE_PHONE,
        )
        assert result.reply_text == orch_mod.SELF_HARM_CRISIS_LINES
        assert "988" in result.reply_text
        assert "team" not in result.reply_text
        assert result.handed_off is False
        assert (await _session_row(s, sid)).mode == "ai"

        asked = await _turn(
            _orch(s, admin, iid),
            sid,
            "can I talk to a real person please",
            channel="sms",
            transport_identifier=JANE_PHONE,
        )
        assert asked.reply_text == orch_mod.TAKEOVER_UNRECORDED_REPLY
        assert asked.handed_off is False
    await escalation_dispatch.drain_background_notifications()
    assert paged == []


# --- finding 8: Luciel's own hand-off and a below-floor draft are ONE page ---------- #
LOYALTY_Q = "do you have a loyalty discount for repeat customers?"
LOYALTY_REASON = "asked about a loyalty discount we have no policy for"
HONEST_DRAFT = "I don't have that information, so I've passed your question to the team."


def _hands_to_team(reason: str = LOYALTY_REASON) -> _CountingEcho:
    """Calls hand_to_team, reads the result back, then says so in words the knowledge
    cannot back (there is nothing in it about discounts): the draft is below the floor."""
    return _CountingEcho(
        confidence=1.0,
        tool_call_script=[[{"tool_id": "hand_to_team", "inputs": {"reason": reason}}]],
        fixed_reply=HONEST_DRAFT,
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_hand_to_team_with_a_below_floor_reply_is_one_row_and_one_page(paged: list[str]):
    """An uncovered question: the model calls hand_to_team (row + page at dispatch)
    and says it passed the question on. Gate 2 scored that draft 0% — hand_to_team is
    not answer-bearing — and wrote a SECOND row, cannot_answer, with a second page:
    the owner was paged twice for one message. The answer-quality reasons now ride on
    the hand-off row this turn already paged; the draft is still replaced."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid, channel="sms")
        result = await _turn(
            _orch(s, admin, iid, primary=_hands_to_team()),
            sid,
            LOYALTY_Q,
            channel="sms",
            transport_identifier=JANE_PHONE,
        )
        assert result.tool_outcomes == ["ok"]
        rows = await _rows(s, sid)
        assert [r.signal for r in rows] == ["luciel_requested_handoff"]  # one row
        reasons = [str(r) for r in (rows[0].reasons or [])]
        assert reasons[:2] == ["Luciel asked for a person", LOYALTY_REASON]
        assert "answer only 0% backed by your knowledge" in reasons
        assert result.escalation is not None
        assert result.escalation.signal is Signal.luciel_requested_handoff
        assert result.escalation.answer_ungrounded is True
        assert HONEST_DRAFT not in result.reply_text  # the below-floor draft never leaves
        assert _HANDOFF_ACK_MARK.search(result.reply_text)
        assert paged == []  # nothing starts before the commit
    await escalation_dispatch.drain_background_notifications()
    assert paged == ["luciel_requested_handoff"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_hand_to_team_on_a_later_uncovered_question_is_that_turns_only_page(
    paged: list[str],
):
    """The session already carries a cannot_answer row from an earlier uncovered
    question. A distinct one now, and the model hands it to the team: that is the
    turn's page. The cannot_answer row used to be extended with 'also asked' and
    re-paged on top of it — two pages for the one message."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid, channel="sms")
        first = await _turn(
            _orch(s, admin, iid), sid, UNCOVERED, channel="sms", transport_identifier=JANE_PHONE
        )
        assert first.escalation is not None
        assert first.escalation.signal is Signal.cannot_answer
    await escalation_dispatch.drain_background_notifications()
    assert paged == ["cannot_answer"]

    async with tenant_session(admin) as s:
        second = await _turn(
            _orch(s, admin, iid, primary=_hands_to_team()),
            sid,
            LOYALTY_Q,
            channel="sms",
            transport_identifier=JANE_PHONE,
        )
        rows = {r.signal: r for r in await _rows(s, sid)}
        assert sorted(rows) == ["cannot_answer", "luciel_requested_handoff"]
        assert not any("also asked" in str(r) for r in (rows["cannot_answer"].reasons or []))
        assert "answer only 0% backed by your knowledge" in [
            str(r) for r in (rows["luciel_requested_handoff"].reasons or [])
        ]
        assert second.escalation is not None
        assert second.escalation.signal is Signal.luciel_requested_handoff
    await escalation_dispatch.drain_background_notifications()
    assert paged == ["cannot_answer", "luciel_requested_handoff"]
