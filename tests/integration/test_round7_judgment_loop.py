"""Luciel's judgment as an employee, through the real loop (round 7 WP-2, D2 2026-09-14).

* A hot lead whose draft is below the grounding floor gets the honest hand-off, one
  escalation row carrying both reasons, and never the fabricated sentence (item 1).
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.models.conversation import EscalationEvent, Transcript
from app.models.conversation import Session as SessionModel
from app.runtime.escalation import Signal
from app.runtime.grounding import GROUNDING_FLOOR
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import Orchestrator
from app.runtime.testing import EchoModel
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

HOT_LEAD = "I have a $5000 budget and want to book a deck build today"
FABRICATED = "Absolutely — we can start Monday and a deck that size runs $4,800 all in."


async def _unrelated_retriever(_q: str) -> list[str]:
    # A chunk that shares almost nothing with the draft: grounding lands just above
    # zero and well below the floor — the live shape of a guess dressed as an answer.
    return ["Our showroom is open Saturdays from 9 to 3 and closed on Sundays."]


async def _seed_session(s, admin, iid, *, channel: str = "widget") -> uuid.UUID:
    sid = uuid.uuid4()
    s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel=channel, mode="ai"))
    await s.flush()
    return sid


def _orch(s, admin, iid, *, primary, retriever=_unrelated_retriever) -> Orchestrator:
    return Orchestrator(
        session=s,
        router=LLMRouter(primary=primary, fallback=EchoModel()),
        retriever=retriever,
        admin_id=admin,
        instance_id=iid,
        enabled_channels={"widget", "voice"},
        has_payment_method=False,
    )


async def _rows(s, sid: uuid.UUID) -> list[EscalationEvent]:
    return list(
        (await s.execute(select(EscalationEvent).where(EscalationEvent.session_id == sid)))
        .scalars()
        .all()
    )


# --- item 1: a hot lead never ships a fabricated answer ------------------------------ #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_hot_lead_with_an_ungrounded_draft_gets_the_honest_ack_not_the_guess():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        orch = _orch(s, admin, iid, primary=EchoModel(confidence=1.0, fixed_reply=FABRICATED))
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text=HOT_LEAD,
        )
        assert result.grounding_score is not None and result.grounding_score < GROUNDING_FLOOR
        # The row is the opportunity signal; the verdict on the draft rides with it.
        assert result.escalation is not None
        assert result.escalation.signal is Signal.high_value_lead
        assert result.escalation.answer_ungrounded is True
        # The fabricated sentence never left; the honest hand-off did.
        assert "$4,800" not in result.reply_text and "start Monday" not in result.reply_text
        assert "team" in result.reply_text.lower()
        # ONE escalation row, carrying both reasons.
        rows = await _rows(s, sid)
        assert [r.signal for r in rows] == ["high_value_lead"]
        reasons = list(rows[0].reasons or [])
        assert "named a budget" in reasons and "ready to buy or book" in reasons
        assert any(r.startswith("answer only") and "backed by your knowledge" in r for r in reasons)
        # The persisted reply is the fixed ack: deterministic and unscored (E2E-6).
        luciel_row = (
            await s.execute(select(Transcript).where(Transcript.id == result.reply_message_id))
        ).scalar_one()
        assert luciel_row.deterministic is True and luciel_row.grounding_score is None
        assert "$4,800" not in luciel_row.text


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_hot_lead_whose_signal_already_fired_still_gets_a_cannot_answer_row():
    """Second turn of the same hot lead: the high_value_lead row is a once-per-session
    event, but this turn's guess is cannot_answer news of its own — never buried."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        orch = _orch(s, admin, iid, primary=EchoModel(confidence=1.0, fixed_reply=FABRICATED))
        for text in (HOT_LEAD, "and how long does a deck like that take you?"):
            result = await orch.run_turn(
                session_id=sid,
                resolved_lead_id=None,
                channel="widget",
                mode="ai",
                inbound_text=text,
            )
            assert "$4,800" not in result.reply_text
        rows = await _rows(s, sid)
        assert sorted(r.signal for r in rows) == ["cannot_answer", "high_value_lead"]
        cannot = next(r for r in rows if r.signal == "cannot_answer")
        assert cannot.reasons and cannot.reasons[0].startswith("answer only")
