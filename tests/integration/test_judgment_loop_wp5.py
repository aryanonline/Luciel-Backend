"""Employee-grade judgment through the real loop (2026-09-05 audit WP5).

* An ungrounded assertion never leaves: the visitor gets the honest hand-off (F100).
* A self-harm message gets compassion, a crisis line and a person at once (F116).
* The fallback model answering a turn is audited (F132).
* A tool whose provider is not connected is not even described to the model.
* The embedding provider failing degrades to an honest no-knowledge turn (F103).
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.knowledge.embeddings import EmbeddingError
from app.models.audit import AdminAuditLog
from app.models.conversation import Session as SessionModel
from app.runtime import knowledge_retrieval
from app.runtime.escalation import Signal
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import SELF_HARM_REPLY, Orchestrator
from app.runtime.testing import EchoModel
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]


async def _empty_retriever(_q: str) -> list[str]:
    return []


class StatementModel(EchoModel):
    """Answers every turn with a confident factual STATEMENT (no tool behind it)."""

    async def complete(self, system_prompt, messages, *, tools=None):
        resp = await super().complete(system_prompt, messages, tools=tools)
        resp.text = "Yes, we carry Rheem and Bradford White heaters and install same day."
        return resp


async def _seed_session(s, admin, iid) -> uuid.UUID:
    sid = uuid.uuid4()
    s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel="widget", mode="ai"))
    await s.flush()
    return sid


def _orch(s, admin, iid, *, primary, fallback=None) -> Orchestrator:
    return Orchestrator(
        session=s,
        router=LLMRouter(primary=primary, fallback=fallback or primary),
        retriever=_empty_retriever,
        admin_id=admin,
        instance_id=iid,
        enabled_channels={"widget"},
        has_payment_method=False,
    )


async def _audit(admin: uuid.UUID, event_type: str) -> list[AdminAuditLog]:
    async with tenant_session(admin) as s:
        return list(
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin, AdminAuditLog.event_type == event_type
                    )
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_ungrounded_assertion_becomes_the_honest_hand_off():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        orch = _orch(s, admin, iid, primary=StatementModel(confidence=0.9))
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="What brands of water heater do you carry",
        )
    assert result.escalation is not None and result.escalation.signal is Signal.cannot_answer
    assert "Rheem" not in result.reply_text  # the guess never left
    assert "team" in result.reply_text.lower()  # the honest hand-off did


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_bare_clarifying_question_still_goes_out_when_ungrounded():
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
    assert "Echo: Do you have any openings?" in result.reply_text


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_self_harm_gets_compassion_a_crisis_line_and_a_person_immediately():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        orch = _orch(s, admin, iid, primary=EchoModel(confidence=0.9))
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="honestly I want to kill myself",
        )
    assert result.reply_text == SELF_HARM_REPLY
    assert "988" in result.reply_text
    assert "Echo:" not in result.reply_text
    assert result.escalation is not None and result.escalation.fired
    assert result.handed_off is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_fallback_model_answering_is_audited():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        sid = await _seed_session(s, admin, iid)
        orch = _orch(
            s,
            admin,
            iid,
            primary=EchoModel(name="primary", raise_provider_error=True),
            fallback=EchoModel(name="fallback", confidence=0.9),
        )
        result = await orch.run_turn(
            session_id=sid,
            resolved_lead_id=None,
            channel="widget",
            mode="ai",
            inbound_text="Do you have any openings?",
        )
    assert result.used_fallback is True
    rows = await _audit(admin, "llm_fallback_used")
    assert len(rows) == 1
    assert rows[0].event_payload["session_id"] == str(sid)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_tool_without_a_connected_provider_is_not_described():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        orch = Orchestrator(
            session=s,
            router=LLMRouter(primary=EchoModel(), fallback=EchoModel()),
            retriever=_empty_retriever,
            admin_id=admin,
            instance_id=iid,
            enabled_channels={"widget"},
            has_payment_method=False,
        )
        # No calendar connection exists → the booking tools are withheld.
        assert await orch._tool_operable("book_appointment") is False
        # send_sms needs the SMS channel ON; the widget-only instance withholds it.
        assert await orch._tool_operable("send_sms") is False
        # A tool with no connection requirement is always describable.
        assert await orch._tool_operable("schedule_callback") is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_embedding_outage_degrades_to_no_knowledge_not_a_crash(monkeypatch):
    from app.knowledge import service

    async def boom(*a, **kw):
        raise EmbeddingError("openai_error:APIConnectionError")

    monkeypatch.setattr(service, "retrieve", boom)
    async with tenant_session(uuid.uuid4()) as s:
        texts, attributed = await knowledge_retrieval.hybrid_retrieve_bundle(
            s, admin_id=uuid.uuid4(), instance_id=uuid.uuid4(), query="opening hours?"
        )
    assert texts == [] and attributed == []
