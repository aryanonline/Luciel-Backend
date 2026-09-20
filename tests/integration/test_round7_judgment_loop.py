"""Luciel's judgment as an employee, through the real loop (round 7 WP-2, D2 2026-09-14).

* A hot lead whose draft is below the grounding floor gets the honest hand-off, one
  escalation row carrying both reasons, and never the fabricated sentence (item 1).
* Luciel's own ``hand_to_team`` call is a real escalation that leaves the session with
  Luciel; a hand-off the model merely PROMISED is made real the same way (item 2).
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
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


# --- item 2: Luciel's own hand-off is real, and a promised one is made real ---------- #
INVOICE_CHUNK = "Invoice disputes are handled by the owner personally."
INVOICE_REPLY = (
    "Invoice disputes are handled by the owner personally - I've passed this to the "
    "team and a person will follow up with you."
)
INVOICE_REASON = "Customer disputes invoice #4471 and wants it waived"


async def _invoice_retriever(_q: str) -> list[str]:
    return [INVOICE_CHUNK]


class _RecordingEcho(EchoModel):
    """EchoModel that keeps every (system_prompt, tools) it was given."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.turns: list[tuple[str, object]] = []

    async def complete(self, system_prompt, messages, *, tools=None):  # type: ignore[override]
        self.turns.append((system_prompt, tools))
        return await super().complete(system_prompt, messages, tools=tools)


def _hand_to_team_model(reason: str = INVOICE_REASON) -> _RecordingEcho:
    return _RecordingEcho(
        confidence=1.0,
        tool_call_script=[[{"tool_id": "hand_to_team", "inputs": {"reason": reason}}]],
        fixed_reply=INVOICE_REPLY,
    )


async def _mode(s, sid: uuid.UUID) -> str:
    return (await s.execute(select(SessionModel).where(SessionModel.id == sid))).scalar_one().mode


async def _fired_signals(s, admin: uuid.UUID) -> list[str]:
    rows = (
        (
            await s.execute(
                select(AdminAuditLog).where(
                    AdminAuditLog.admin_id == admin,
                    AdminAuditLog.event_type == "escalation_fired",
                )
            )
        )
        .scalars()
        .all()
    )
    return [str(r.event_payload["signal"]) for r in rows]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_hand_to_team_records_a_real_escalation_and_luciel_keeps_answering():
    """No tools wired at all (tool_resolver=None): the cognition tool is still
    described, still dispatchable, and its execution is the standard escalation."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        model = _hand_to_team_model()
        orch = _orch(s, admin, iid, primary=model, retriever=_invoice_retriever)
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="I want to dispute invoice #4471, the amount is wrong",
        )
        # The model was told about the tool on the very first call, no catalog needed.
        prompt, tools = model.turns[0]
        assert "- hand_to_team:" in prompt
        assert [t["tool_id"] for t in tools] == ["hand_to_team"]
        # The tool ran through the loop's own dispatch: ok, read back to the model.
        assert result.tool_outcomes == ["ok"]
        # The turn's escalation is Luciel's own; the session is NOT taken away (D2).
        assert result.escalation is not None
        assert result.escalation.signal is Signal.luciel_requested_handoff
        assert result.handed_off is False
        assert await _mode(s, sid) == "ai"
        # The reply is the model's own (grounded) wording, not a canned line.
        assert "passed this to the team" in result.reply_text
        rows = await _rows(s, sid)
        assert [r.signal for r in rows] == ["luciel_requested_handoff"]
        assert rows[0].gate == "outcome"
        assert list(rows[0].reasons or []) == ["Luciel asked for a person", INVOICE_REASON]
        assert await _fired_signals(s, admin) == ["luciel_requested_handoff"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_hand_to_team_is_outside_every_tool_gate_and_fires_once_per_session():
    """A resolver that binds nothing and an EMPTY capability snapshot: every catalog
    tool is refused, hand_to_team still runs. A second call in the same conversation
    writes no second row and pages nobody twice."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        orch = _orch(s, admin, iid, primary=_hand_to_team_model(), retriever=_invoice_retriever)
        orch.tool_resolver = lambda _tid: None
        orch.snapshot_tool_ids = set()
        first = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="I want to dispute invoice #4471, the amount is wrong",
        )
        assert first.tool_outcomes == ["ok"]
        orch2 = _orch(
            s, admin, iid, primary=_hand_to_team_model("still upset"), retriever=_invoice_retriever
        )
        second = await orch2.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="the invoice #4471 dispute again, any news?",
        )
        assert second.tool_outcomes == ["ok"]
        assert second.escalation is not None
        assert second.escalation.signal is Signal.luciel_requested_handoff
        assert [r.signal for r in await _rows(s, sid)] == ["luciel_requested_handoff"]
        assert await _fired_signals(s, admin) == ["luciel_requested_handoff"]
        assert await _mode(s, sid) == "ai"


SHOWROOM_CHUNK = "Our showroom is open Saturdays from 9 to 3 and closed on Sundays."
PROMISED_REPLY = (
    "Our showroom is open Saturdays from 9 to 3 and closed on Sundays. I've passed "
    "your feedback along to the team and someone will get back to you."
)
FACT_REPLY = "You can reach the team directly at 555-0100 whenever you like."


async def _showroom_retriever(_q: str) -> list[str]:
    return [SHOWROOM_CHUNK]


async def _contact_retriever(_q: str) -> list[str]:
    return ["You can reach the team directly at 555-0100."]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_promised_handoff_without_the_tool_becomes_a_real_escalation():
    """The model wrote "I've passed your feedback along … someone will get back to
    you" and called nothing. The customer was promised a person: the promise is
    recorded through the same path the tool uses, the owner is paged, and the
    reply is left as it is — now true."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        orch = _orch(
            s,
            admin,
            iid,
            primary=EchoModel(confidence=1.0, fixed_reply=PROMISED_REPLY),
            retriever=_showroom_retriever,
        )
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text=(
                "I'd like to leave feedback: the parking was awful. Also, is the "
                "showroom open Saturdays?"
            ),
        )
        assert result.grounding_score is not None and result.grounding_score >= GROUNDING_FLOOR
        assert result.tool_outcomes == []
        assert "passed your feedback along to the team" in result.reply_text
        assert result.escalation is not None
        assert result.escalation.signal is Signal.luciel_requested_handoff
        assert result.handed_off is False
        assert await _mode(s, sid) == "ai"
        rows = await _rows(s, sid)
        assert [r.signal for r in rows] == ["luciel_requested_handoff"]
        assert list(rows[0].reasons or []) == ["Luciel told the customer the team would follow up"]
        assert await _fired_signals(s, admin) == ["luciel_requested_handoff"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_fact_about_reaching_the_team_is_not_a_handoff_claim():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        orch = _orch(
            s,
            admin,
            iid,
            primary=EchoModel(confidence=1.0, fixed_reply=FACT_REPLY),
            retriever=_contact_retriever,
        )
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="How do I contact the team?",
        )
        assert result.escalation is None
        assert FACT_REPLY in result.reply_text
        assert await _rows(s, sid) == []
