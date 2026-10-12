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
