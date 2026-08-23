"""Cognition fixes from audit round 3, C5.

  * Voice parity: the voice pipeline builds its Orchestrator WITH the tool
    resolver + snapshot capability set (it used to build bare — every proposed
    tool on a phone call resolved to `failed` while text channels dispatched).
  * At-cap lead capture: deterministic capture runs BEFORE the budget gate, so
    the at-cap escalation row carries the lead ("leads still captured +
    escalated" at cap) — no LLM call is added.
  * Gate-1 contact ask: an anonymous widget visitor who asks for a human gets
    the contact ask appended to the handoff ack (previously only gate-2 asked).
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.models.conversation import EscalationEvent
from app.models.conversation import Session as SessionModel
from app.models.instance import Instance, LucielState
from app.models.lead import Lead
from app.runtime import budget
from app.runtime import voice_pipeline as vp
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import Orchestrator
from app.runtime.speech import EchoSpeechToText, EchoTextToSpeech
from app.runtime.testing import EchoModel
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]


async def _empty_retriever(_q: str) -> list[str]:
    return []


def _orch(s, admin, iid, *, channel_set=None) -> Orchestrator:
    return Orchestrator(
        session=s,
        router=LLMRouter(primary=EchoModel(confidence=0.9), fallback=EchoModel()),
        retriever=_empty_retriever,
        admin_id=admin,
        instance_id=iid,
        enabled_channels=channel_set or {"widget"},
        has_payment_method=False,
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_voice_pipeline_wires_the_tool_resolver(monkeypatch):
    """The C5 parity fix, pinned at the construction site: handle_turn builds its
    Orchestrator with a real tool_resolver and the snapshot capability set."""
    captured: dict[str, object] = {}

    class _CapturingOrchestrator(Orchestrator):
        def __post_init__(self) -> None:  # keep prompt composition
            super().__post_init__()
            captured["tool_resolver"] = self.tool_resolver
            captured["snapshot_tool_ids"] = self.snapshot_tool_ids

    monkeypatch.setattr(vp, "Orchestrator", _CapturingOrchestrator)
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        inst = Instance(
            id=iid,
            admin_id=admin,
            name="L",
            state=LucielState.active,
            channels=["voice"],
            tools=[],
            escalation={},
            personality={},
        )
        s.add(inst)
        await s.flush()
        echo = EchoModel(confidence=0.9)
        await vp.handle_turn(
            s,
            instance=inst,
            admin_id=admin,
            instance_id=iid,
            from_number="+15557654321",
            # EchoSpeechToText treats the audio_ref AS the transcript.
            speech_audio_ref="what are your hours",
            action_url="https://api.vantagemind.com/voice/turn",
            stt=EchoSpeechToText(),
            tts=EchoTextToSpeech(),
            router=LLMRouter(primary=echo, fallback=echo),
            has_payment_method=False,
        )
    assert captured["tool_resolver"] is not None, (
        "voice must wire the tool resolver — a caller could never book otherwise (C5)"
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_at_cap_turn_still_captures_the_lead_and_links_the_escalation():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        state0 = await budget.check_budget(s, admin_id=admin, instance_id=iid)
        for _ in range(state0.free_allowance):
            await budget.count_session_once(
                s, admin_id=admin, instance_id=iid, session_id=uuid.uuid4()
            )
        sid = uuid.uuid4()
        s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel="sms", mode="ai"))
        await s.flush()
        result = await _orch(s, admin, iid, channel_set={"sms"}).run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="sms",
            mode="ai",
            # Strong channel + qualified intent (budget figure) → capture fires.
            inbound_text="My budget is $5000 — can you call me about the premium package?",
            transport_identifier="+15550100200",
        )
        assert result.at_cap is True
        lead = (
            await s.execute(select(Lead).where(Lead.admin_id == admin))
        ).scalar_one_or_none()
        assert lead is not None, "capture is cognition — it must run even at cap (§3.4.1b)"
        ev = (
            (await s.execute(select(EscalationEvent).where(EscalationEvent.session_id == sid)))
            .scalars()
            .all()
        )
        assert ev and any(
            e.signal == "budget_exhausted" and e.resolved_lead_id == lead.id for e in ev
        ), "the at-cap escalation row must carry the captured lead id (C5)"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_gate1_human_request_from_anonymous_widget_asks_for_contact():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = uuid.uuid4()
        s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel="widget", mode="ai"))
        await s.flush()
        result = await _orch(s, admin, iid).run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="I want to speak to a human please",
        )
        assert result.handed_off is True
        assert "what's your name" in result.reply_text, (
            "gate-1 was the most likely escalation for an anonymous visitor and the "
            "only one that didn't ask for contact info (C5)"
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_gate1_from_strong_channel_does_not_ask_for_contact():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = uuid.uuid4()
        s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel="sms", mode="ai"))
        await s.flush()
        result = await _orch(s, admin, iid, channel_set={"sms"}).run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="sms",
            mode="ai",
            inbound_text="I want to speak to a human please",
            transport_identifier="+15550100200",
        )
        assert result.handed_off is True
        assert "what's your name" not in result.reply_text, (
            "the transport already names the person on a strong channel"
        )
