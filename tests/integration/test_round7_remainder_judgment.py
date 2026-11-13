"""Judgment as an employee, the remainder (round 7 WP-2): the loop through the real
orchestrator with the deterministic EchoModel.

* Item 1: an anonymous widget visitor whose three questions the knowledge cannot
  cover hears the full hand-off line and the contact ask ONCE, then two short, varied
  acknowledgements; the session keeps one escalation row, and the owner is paged once
  per DISTINCT uncovered question (the new question rides on the row's reasons) —
  never for a repeat.
* Item 2 (§3.3.7 rule 4): a customer who declines a detail a tool needs is not asked
  again in other words — the ask stops in code; a blocked action becomes a real
  hand-off naming the field; a prose stall just moves on.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from app.db.session import tenant_session
from app.models.conversation import EscalationEvent
from app.models.conversation import Session as SessionModel
from app.models.instance import Instance, LucielState
from app.models.tool_auth import InstanceToolAuthorization
from app.notifications import email_notifier, escalation_dispatch
from app.runtime.escalation import Signal
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import (
    CONTACT_ASK_LINE,
    DECLINED_INPUT_CONTINUE,
    DECLINED_INPUT_LINE,
    SHORT_HANDOFF_ACKS,
    Orchestrator,
    PendingGather,
    declined_input_reply,
    handoff_ack_sent,
)
from app.runtime.testing import EchoModel
from app.tools import contract as tool_contract
from sqlalchemy import select

CONTACT = "frontdesk@brightsmile.example"
# A statement, not a question: the model's draft is a guess with no knowledge behind it,
# so the below-floor replacement runs (a bare clarifying question would ship as-is, F100).
GUESS = "We do those from $900 and there is parking behind the building."


class _Sender:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, *, to, subject, body_text, from_address=None, tags=None):
        self.sent.append(to)
        return f"ses-{len(self.sent)}"


@pytest.fixture
def ses(monkeypatch) -> _Sender:
    fake = _Sender()
    monkeypatch.setattr(email_notifier, "build_email_sender", lambda: fake)
    return fake


async def _empty_retriever(_q: str) -> list[str]:
    return []


async def _seed(s, admin: uuid.UUID, iid: uuid.UUID) -> uuid.UUID:
    s.add(
        Instance(
            id=iid,
            admin_id=admin,
            name="Bright Smile Dental",
            state=LucielState.active,
            escalation={"primaryEmail": CONTACT, "preferredChannel": "email"},
        )
    )
    sid = uuid.uuid4()
    s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel="widget", mode="ai"))
    await s.flush()
    return sid


def _orch(s, admin: uuid.UUID, iid: uuid.UUID) -> Orchestrator:
    primary = EchoModel(confidence=1.0, fixed_reply=GUESS)
    return Orchestrator(
        session=s,
        router=LLMRouter(primary=primary, fallback=primary),
        retriever=_empty_retriever,
        admin_id=admin,
        instance_id=iid,
        enabled_channels={"widget"},
        has_payment_method=False,
    )


async def _turns(s, orch: Orchestrator, sid: uuid.UUID, questions: list[str]) -> list[str]:
    replies: list[str] = []
    for text in questions:
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text=text,
            # The adapters stamp the inbound's TRUE arrival time; without it every lead
            # row of one transaction shares the transaction timestamp and the transcript
            # order the acks are read from collapses.
            received_at=datetime.now(UTC),
        )
        assert result.escalation is not None and result.handed_off is False
        replies.append(result.reply_text)
    return replies


async def _rows(s, sid: uuid.UUID) -> list[EscalationEvent]:
    return list(
        (await s.execute(select(EscalationEvent).where(EscalationEvent.session_id == sid)))
        .scalars()
        .all()
    )


# --- item 1 ------------------------------------------------------------------------ #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_three_uncovered_questions_get_one_full_ack_then_short_ones(ses: _Sender):
    """The full hand-off paragraph and the contact ask used to go out on EVERY
    below-floor turn — the visitor read the identical block three times, and the
    second and third questions never reached the owner because the row was already
    written. Now: one full ack + one ask, two short varied acks, one row carrying
    every question, one page per distinct question."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    questions = ["do you do implants?", "how much are crowns?", "do you open saturdays?"]
    async with tenant_session(admin) as s:
        sid = await _seed(s, admin, iid)
        first, second, third = await _turns(s, _orch(s, admin, iid), sid, questions)

        # Turn 1: the full ack and the contact ask (the anonymous visitor is unreachable).
        assert handoff_ack_sent([SimpleNamespace(role="luciel", text=first)])
        assert CONTACT_ASK_LINE in first
        assert GUESS not in first
        # Turns 2 and 3: short, different, and no nag for contact details.
        assert second.startswith(SHORT_HANDOFF_ACKS[0])
        assert third.startswith(SHORT_HANDOFF_ACKS[1])
        for later in (second, third):
            assert CONTACT_ASK_LINE not in later
            assert "flagged this for the team" not in later
            assert GUESS not in later
        assert len({first, second, third}) == 3  # never the same paragraph three times

        # ONE row for the session; every later distinct question is on its reasons.
        (row,) = await _rows(s, sid)
        assert row.signal == "cannot_answer"
        reasons = list(row.reasons or [])
        assert 'also asked: "how much are crowns?"' in reasons
        assert 'also asked: "do you open saturdays?"' in reasons

    # Delivery runs after the commit, off the customer's turn: one page for the row,
    # one re-notification per distinct question the visitor went on to ask.
    await escalation_dispatch.drain_background_notifications()
    assert ses.sent == [CONTACT, CONTACT, CONTACT]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_repeated_uncovered_question_pages_nobody_again(ses: _Sender):
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed(s, admin, iid)
        first, second = await _turns(
            s, _orch(s, admin, iid), sid, ["do you do implants?", "implants??"]
        )
        assert CONTACT_ASK_LINE in first and CONTACT_ASK_LINE not in second
        assert second.startswith(SHORT_HANDOFF_ACKS[0])  # still a short ack, not silence
        (row,) = await _rows(s, sid)
        assert not any(str(r).startswith("also asked") for r in row.reasons or [])
    await escalation_dispatch.drain_background_notifications()
    assert ses.sent == [CONTACT]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_contact_ask_is_not_repeated_while_the_visitor_stays_anonymous(ses: _Sender):
    """Five uncovered questions in a row: the ask goes out on the first and — with
    still nothing on file — once more after the respite, never on every turn."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    questions = [
        "do you do implants?",
        "how much are crowns?",
        "do you open saturdays?",
        "is parking free?",
        "do you take insurance?",
        "can kids come too?",
    ]
    async with tenant_session(admin) as s:
        sid = await _seed(s, admin, iid)
        replies = await _turns(s, _orch(s, admin, iid), sid, questions)
    asked = [CONTACT_ASK_LINE in r for r in replies]
    assert asked == [True, False, False, False, False, True]


# --- item 2: taking no for an answer (§3.3.7 rule 4) --------------------------------- #
class _FakeTool:
    requires_connection: str | None = None
    requires_channels: frozenset[str] = frozenset()
    execution_mode = "in_process"
    display_name = "Fake"
    description = "fake"

    def __init__(self, tool_id: str, required_inputs: frozenset[str]) -> None:
        self.tool_id = tool_id
        self.required_inputs = required_inputs
        self.calls: list[dict] = []

    async def execute(self, inputs, context):
        self.calls.append(dict(inputs))
        return tool_contract.ok({"bookingRef": "bk-1"})


class _RecordingEcho(EchoModel):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.calls = 0

    async def complete(self, system_prompt, messages, *, tools=None):  # type: ignore[override]
        self.calls += 1
        return await super().complete(system_prompt, messages, tools=tools)


HOURS_CHUNK = "Opening hours: we open 9am to 5pm Monday to Friday and 10am to 2pm on Saturdays."
HOURS_ANSWER = "We open 9am to 5pm Monday to Friday and 10am to 2pm on Saturdays."


async def _hours_retriever(_q: str) -> list[str]:
    return [HOURS_CHUNK]


def _tool_orch(
    s, admin, iid, *, primary, tool: _FakeTool | None, retriever=_empty_retriever
) -> Orchestrator:
    return Orchestrator(
        session=s,
        router=LLMRouter(primary=primary, fallback=EchoModel()),
        retriever=retriever,
        admin_id=admin,
        instance_id=iid,
        enabled_channels={"widget"},
        tool_resolver=lambda tool_id: (
            tool if tool is not None and tool.tool_id == tool_id else None
        ),
        has_payment_method=False,
    )


async def _run(orch: Orchestrator, sid: uuid.UUID, text: str):
    return await orch.run_turn(
        session_id=sid,
        resolved_lead_id=None,
        channel="widget",
        mode="ai",
        inbound_text=text,
        received_at=datetime.now(UTC),
    )


async def _session_row(s, sid: uuid.UUID) -> SessionModel:
    return (await s.execute(select(SessionModel).where(SessionModel.id == sid))).scalar_one()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_declined_required_input_stops_the_ask_and_hands_off_naming_the_field(
    ses: _Sender,
):
    """The broker refused book_appointment for want of an email and Luciel asked; the
    customer declines. Rule 4: no re-ask in fresh words — the ask stops in code, the
    booking is said to be blocked, and the owner reads WHICH field was declined."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed(s, admin, iid)
        tool = _FakeTool("book_appointment", frozenset({"email"}))
        s.add(
            InstanceToolAuthorization(
                admin_id=admin, instance_id=iid, tool_id=tool.tool_id, enabled=True
            )
        )
        await s.flush()
        asked = await _run(
            _tool_orch(
                s,
                admin,
                iid,
                primary=EchoModel(
                    tool_call_script=[
                        [
                            {
                                "tool_id": "book_appointment",
                                "inputs": {"slot": "2026-10-01T14:00:00"},
                            }
                        ]
                    ],
                    fixed_reply="What email should I send the confirmation to?",
                ),
                tool=tool,
            ),
            sid,
            "book me for 2pm on the 1st",
        )
        assert asked.tool_outcomes == ["missing_input"]
        assert (await _session_row(s, sid)).input_ask_attempts == 1

        model = _RecordingEcho(fixed_reply="Sure — what's the best email for you?")
        declined = await _run(
            _tool_orch(s, admin, iid, primary=model, tool=tool), sid, "I'd rather not give my email"
        )
        # No model call: the reply is composed in code and never re-asks.
        assert model.calls == 0 and declined.iterations == 0
        assert declined.reply_text == declined_input_reply(
            PendingGather("book_appointment", ("email",))
        )
        assert "?" not in declined.reply_text
        assert "the booking" in declined.reply_text
        assert declined.escalation is not None
        assert declined.escalation.signal is Signal.cannot_answer
        assert "customer declined to give email" in declined.escalation.reasons
        assert (await _session_row(s, sid)).input_ask_attempts == 0
        assert (await _session_row(s, sid)).mode == "ai"
        rows = await _rows(s, sid)
        assert [r.signal for r in rows] == ["cannot_answer"]
        assert "customer declined to give email" in list(rows[0].reasons or [])
        assert "the booking cannot go ahead without it" in list(rows[0].reasons or [])
        assert tool.calls == []  # nothing was ever booked with an invented value

        # The next turn is answered by Luciel as usual — a covered question gets the
        # grounded answer, with no re-ask riding on it.
        after = await _run(
            _tool_orch(
                s,
                admin,
                iid,
                primary=EchoModel(confidence=1.0, fixed_reply=HOURS_ANSWER),
                tool=tool,
                retriever=_hours_retriever,
            ),
            sid,
            "ok, what are your hours?",
        )
        assert after.iterations == 1 and HOURS_ANSWER in after.reply_text
        assert after.escalation is None and "?" not in after.reply_text
    await escalation_dispatch.drain_background_notifications()
    assert CONTACT in ses.sent  # the owner was paged about the stalled booking


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_decline_on_a_prose_stall_just_moves_on():
    """No tool was blocked (the model asked in prose): the ask stops, nothing is
    escalated, the counter resets, and no model call is made on the decline."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed(s, admin, iid)
        (await _session_row(s, sid)).input_ask_attempts = 1
        await s.flush()
        model = _RecordingEcho()
        result = await _run(_tool_orch(s, admin, iid, primary=model, tool=None), sid, "no thanks")
        assert model.calls == 0
        assert result.reply_text == DECLINED_INPUT_CONTINUE
        assert result.escalation is None
        assert (await _session_row(s, sid)).input_ask_attempts == 0
        assert await _rows(s, sid) == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_decline_that_carries_a_question_is_answered_after_taking_no():
    """A covered question in the same breath as the decline: the loop answers it from
    the knowledge, and the reply opens by taking no for an answer."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed(s, admin, iid)
        (await _session_row(s, sid)).input_ask_attempts = 1
        await s.flush()
        model = _RecordingEcho(confidence=1.0, fixed_reply=HOURS_ANSWER)
        result = await _run(
            _tool_orch(s, admin, iid, primary=model, tool=None, retriever=_hours_retriever),
            sid,
            "I'd rather not say. what are your opening hours?",
        )
        assert model.calls == 1  # the question is answered by the loop
        assert result.escalation is None
        assert DECLINED_INPUT_LINE in result.reply_text
        assert HOURS_ANSWER in result.reply_text
        assert result.reply_text.index(DECLINED_INPUT_LINE) < result.reply_text.index(HOURS_ANSWER)
        assert CONTACT_ASK_LINE not in result.reply_text
        assert (await _session_row(s, sid)).input_ask_attempts == 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_decline_with_an_uncovered_question_hands_off_without_re_asking(ses: _Sender):
    """The decline rides with a question the knowledge cannot cover: the honest
    hand-off line still opens by taking no for an answer, and the contact ask — the
    very identifiers the customer just declined — is NOT appended, neither on this
    turn nor on the session's later uncovered questions (Luciel said it would not ask
    again)."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed(s, admin, iid)
        (await _session_row(s, sid)).input_ask_attempts = 1
        await s.flush()
        orch = _orch(s, admin, iid)  # empty retriever, the GUESS draft
        first = await _run(orch, sid, "I'd rather not say. do you do implants?")
        assert first.escalation is not None and first.escalation.signal is Signal.cannot_answer
        assert first.handed_off is False
        assert DECLINED_INPUT_LINE in first.reply_text
        assert GUESS not in first.reply_text  # the guess never leaves
        assert CONTACT_ASK_LINE not in first.reply_text
        assert (await _session_row(s, sid)).input_ask_attempts == 0

        second = await _run(orch, sid, "and how much are crowns?")
        assert second.escalation is not None
        assert CONTACT_ASK_LINE not in second.reply_text
        assert any(second.reply_text.startswith(ack) for ack in SHORT_HANDOFF_ACKS)
        assert DECLINED_INPUT_LINE not in second.reply_text  # said once, on the decline
        assert [r.signal for r in await _rows(s, sid)] == ["cannot_answer"]
    await escalation_dispatch.drain_background_notifications()
    assert ses.sent.count(CONTACT) == 2  # the row, then the distinct new question


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_outside_a_pending_gather_a_decline_phrase_is_ordinary_conversation():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed(s, admin, iid)
        model = _RecordingEcho(confidence=1.0)
        result = await _run(_tool_orch(s, admin, iid, primary=model, tool=None), sid, "no thanks")
        assert model.calls == 1
        assert DECLINED_INPUT_LINE not in result.reply_text
