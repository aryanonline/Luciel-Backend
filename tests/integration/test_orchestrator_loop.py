"""Agentic loop end-to-end with the deterministic EchoModel (Architecture §3.4.1).

Exercises the loop hermetically: budget gate, intake escalation, normal answer +
fire-once counting, grounding, the at-cap graceful no-LLM reply, llm_unavailable
failover, and the human_controlled stop.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.models.conversation import EscalationEvent, Transcript
from app.models.conversation import Session as SessionModel
from app.models.instance import Instance, LucielState
from app.runtime import budget
from app.runtime.escalation import Signal
from app.runtime.grounding import GROUNDING_FLOOR
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import AT_CAP_REPLY, Orchestrator
from app.runtime.testing import EchoModel
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]


async def _empty_retriever(_q: str) -> list[str]:
    return []


async def _seed_session(s, admin, iid, *, mode="ai") -> uuid.UUID:
    sid = uuid.uuid4()
    s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel="widget", mode=mode))
    await s.flush()
    return sid


def _orch(s, admin, iid, *, primary, fallback=None, has_pm=False) -> Orchestrator:
    return Orchestrator(
        session=s,
        router=LLMRouter(primary=primary, fallback=fallback or primary),
        retriever=_empty_retriever,
        admin_id=admin,
        instance_id=iid,
        enabled_channels={"widget"},
        has_payment_method=has_pm,
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_normal_turn_answers_and_counts_once():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        orch = _orch(s, admin, iid, primary=EchoModel(confidence=0.9))
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="Do you have any openings?",
        )
        # First turn on a text channel carries the AI-identity disclosure backstop
        # (§3.4.16), then the (echoed) answer content.
        assert "ai assistant" in result.reply_text.lower()
        assert "Echo:" in result.reply_text
        assert result.counted is True
        assert result.at_cap is False
        # The transcript persisted both the lead message and the luciel reply.
        rows = (
            (await s.execute(select(Transcript).where(Transcript.session_id == sid)))
            .scalars()
            .all()
        )
        roles = sorted(r.role for r in rows)
        assert roles == ["lead", "luciel"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_explicit_human_request_hands_off_at_intake():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        orch = _orch(s, admin, iid, primary=EchoModel())
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="I want to speak to a human please",
        )
        assert result.handed_off is True
        assert result.escalation is not None
        assert result.escalation.signal is Signal.explicit_human_request
        # An escalation event row was written at the intake gate (§3.5.2).
        ev = (
            (await s.execute(select(EscalationEvent).where(EscalationEvent.session_id == sid)))
            .scalars()
            .all()
        )
        assert any(e.gate == "intake" for e in ev)
        # Session is now human_controlled (§3.4.12).
        sess = (await s.execute(select(SessionModel).where(SessionModel.id == sid))).scalar_one()
        assert sess.mode == "human_controlled"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_intake_handoff_ack_is_honest_when_no_escalation_contact_configured():
    """Harmony fix 2026-08-04 item 4: with no Instance row at all (the common case in
    these hermetic tests, and a real brand-new account before onboarding), the ack must
    not promise a live human is connecting right now — the only thing that will
    actually happen is a best-effort email to the owner's own account address whenever
    they next check it."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        orch = _orch(s, admin, iid, primary=EchoModel())
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="I want to speak to a human please",
        )
        assert "right now" not in result.reply_text.lower()
        assert "connecting" not in result.reply_text.lower()
        assert "team" in result.reply_text.lower()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_intake_handoff_ack_promises_immediacy_only_when_a_contact_is_configured():
    """The flip side: once the admin has actually configured a primary contact,
    escalations really do reach someone promptly, so the more immediate copy is honest
    and should be used."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="L",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={"primaryEmail": "owner@acme.example"},
                personality={},
            )
        )
        await s.flush()
        sid = await _seed_session(s, admin, iid)
        orch = _orch(s, admin, iid, primary=EchoModel())
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="I want to speak to a human please",
        )
        assert "right now" in result.reply_text.lower()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_at_cap_returns_graceful_reply_without_counting():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        # Drive the tenant to the cap first.
        state0 = await budget.check_budget(s, admin_id=admin, instance_id=iid)
        for _ in range(state0.free_allowance):
            await budget.count_session_once(
                s, admin_id=admin, instance_id=iid, session_id=uuid.uuid4()
            )
        sid = await _seed_session(s, admin, iid)
        orch = _orch(s, admin, iid, primary=EchoModel(), has_pm=False)
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="hello",
        )
        # Graceful at-cap reply, no LLM, no increment (§3.4.1b).
        assert result.at_cap is True
        assert result.reply_text == AT_CAP_REPLY
        assert result.counted is False
        state = await budget.check_budget(s, admin_id=admin, instance_id=iid)
        assert state.counted == state.free_allowance  # unchanged by the capped turn


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_both_providers_down_escalates_llm_unavailable():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        down = EchoModel(raise_provider_error=True)
        orch = _orch(s, admin, iid, primary=down, fallback=down)
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="any availability?",
        )
        # No served turn → not counted; cannot_answer escalation (§3.4.5 server signal).
        assert result.counted is False
        assert result.escalation is not None
        ev = (
            (await s.execute(select(EscalationEvent).where(EscalationEvent.session_id == sid)))
            .scalars()
            .all()
        )
        assert any(e.signal == "llm_unavailable" for e in ev)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_human_controlled_session_stops_the_loop():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid, mode="human_controlled")
        orch = _orch(s, admin, iid, primary=EchoModel())
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="human_controlled",
            inbound_text="still there?",
        )
        # No LLM reply; inbound persisted to the live feed only (§3.4.12).
        assert result.handed_off is True
        assert result.reply_text == ""
        rows = (
            (await s.execute(select(Transcript).where(Transcript.session_id == sid)))
            .scalars()
            .all()
        )
        assert [r.role for r in rows] == ["lead"]  # inbound only, no luciel turn


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_high_value_lead_escalates_at_outcome():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        orch = _orch(s, admin, iid, primary=EchoModel(confidence=0.9))
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="I have a $5000 budget and want to book today",
        )
        assert result.escalation is not None
        assert result.escalation.signal is Signal.high_value_lead
        assert result.counted is True  # answered + counted, then escalated at outcome


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_kb_backed_answer_scores_above_floor_and_persists_the_real_score():
    """Harmony fix 2026-08-04 (item 3): a KB-backed answer whose reply is actually
    supported by retrieved chunks must score above the grounding floor, and that REAL
    score (not a hardcoded 0.50) must be what's persisted on the transcript row for
    the dashboard evidence endpoint to read."""
    admin, iid = uuid.uuid4(), uuid.uuid4()

    async def _kb_retriever(_q: str) -> list[str]:
        return [
            "Our return policy allows returns within 30 days of purchase with a receipt.",
            "Electronics have a 15 day return window due to manufacturer restrictions.",
        ]

    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        primary = EchoModel(
            confidence=1.0,  # matches what live providers always report (§3.4.3)
            fixed_reply="Electronics have a 15 day return window per our policy.",
        )
        orch = Orchestrator(
            session=s,
            router=LLMRouter(primary=primary, fallback=primary),
            retriever=_kb_retriever,
            admin_id=admin,
            instance_id=iid,
            enabled_channels={"widget"},
            has_payment_method=False,
        )
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="What's your return policy for electronics?",
        )

        assert result.escalation is None, "a grounded answer must not escalate"
        assert result.grounding_score >= GROUNDING_FLOOR

        luciel_row = (
            await s.execute(
                select(Transcript).where(
                    Transcript.session_id == sid, Transcript.role == "luciel"
                )
            )
        ).scalar_one()
        assert luciel_row.grounding_score == pytest.approx(result.grounding_score)
        assert luciel_row.grounding_score >= GROUNDING_FLOOR


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_no_kb_confidently_fabricated_answer_escalates_instead_of_confident_specifics():
    """The exact walkthrough symptom: a no-KB question answered with confident,
    specific-sounding fabrication (a fake warranty claim) must escalate rather than
    reach the customer as a confident answer -- even though the live-provider
    confidence signal is pinned at 1.0 (app/runtime/providers.py), exactly like the
    real Anthropic/OpenAI providers report today. Before the harmony fix, the AND
    gate meant this NEVER escalated because plan_confidence=1.0 is never < 0.6."""
    admin, iid = uuid.uuid4(), uuid.uuid4()

    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        primary = EchoModel(
            confidence=1.0,
            fixed_reply="Sure, we offer a lifetime warranty on all products, no questions asked.",
        )
        orch = _orch(s, admin, iid, primary=primary)
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="Do you offer a warranty?",
        )

        assert result.escalation is not None, (
            "a confidently fabricated, ungrounded answer must escalate — this is the "
            "harmony fix item 3 regression test"
        )
        assert result.escalation.signal is Signal.cannot_answer
        assert result.grounding_score == 0.0

        luciel_row = (
            await s.execute(
                select(Transcript).where(
                    Transcript.session_id == sid, Transcript.role == "luciel"
                )
            )
        ).scalar_one()
        assert luciel_row.grounding_score == 0.0
