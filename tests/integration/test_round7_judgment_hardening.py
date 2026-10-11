"""Luciel's runtime judgment, hardened (round 7, review:judgment findings).

Every test here drives the Orchestrator through real turns, the way the established
loop tests do — a deterministic model double, a fake retriever, the tenant session —
and asserts on what the customer read, what the owner was paged, and what the rows
say. Nothing is mocked below the loop except the outbound senders.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.db.session import tenant_session
from app.models.conversation import EscalationEvent
from app.models.conversation import Session as SessionModel
from app.runtime.escalation import Signal
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import (
    _HANDOFF_ACK_MARK,
    SHORT_HANDOFF_ACKS,
    Orchestrator,
    handoff_ack_sent,
)
from app.runtime.testing import EchoModel
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
