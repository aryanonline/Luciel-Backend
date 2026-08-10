"""Cross-session memory injection (§3.4.6) — the returning lead is remembered.

Until the orchestrator called ``load_cross_session_summaries``, the summary store was
write-only: summaries existed (once finalization ran) but no turn ever read them, so
"Luciel remembers past conversations" was doctrine without a mechanism. These tests
pin the injection: prior summaries enter the model context — wrapped as UNTRUSTED
data (§3.4.14 Defense 2), before this session's history — for a resolved lead on a
memory-carrying channel, and never for the weak widget identity (§3.4.9 HARD RULE).
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.models.conversation import Session as SessionModel
from app.models.conversation import SessionSummary
from app.models.instance import Instance, LucielState
from app.runtime.llm_router import LLMResponse, LLMRouter, ModelClass
from app.runtime.orchestrator import Orchestrator

pytestmark = [pytest.mark.asyncio]


class CapturingModel:
    """LLMProvider double that records the message list each completion received."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, system_prompt: str, messages: list[dict[str, str]]) -> LLMResponse:
        self.calls.append([dict(m) for m in messages])
        return LLMResponse(
            text="Happy to help again!",
            provider="capture",
            model_class=ModelClass.primary,
            confidence=0.9,
            tool_calls=[],
        )


async def _empty_retriever(_q: str) -> list[str]:
    return []


async def _seed(admin: uuid.UUID, iid: uuid.UUID) -> None:
    async with tenant_session(admin) as s:
        s.add(Instance(id=iid, admin_id=admin, name="Acme", state=LucielState.active))
        await s.flush()


async def _seed_prior_summary(
    admin: uuid.UUID, iid: uuid.UUID, lead_id: uuid.UUID, text: str
) -> None:
    async with tenant_session(admin) as s:
        s.add(
            SessionSummary(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                resolved_lead_id=lead_id,
                session_id=uuid.uuid4(),
                summary=text,
            )
        )
        await s.flush()


async def _run_turn(
    admin: uuid.UUID,
    iid: uuid.UUID,
    *,
    channel: str,
    lead_id: uuid.UUID | None,
    model: CapturingModel,
) -> None:
    async with tenant_session(admin) as s:
        sid = uuid.uuid4()
        s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel=channel, mode="ai"))
        await s.flush()
        orch = Orchestrator(
            session=s,
            router=LLMRouter(primary=model, fallback=model),
            retriever=_empty_retriever,
            admin_id=admin,
            instance_id=iid,
            enabled_channels={channel},
            has_payment_method=False,
        )
        await orch.run_turn(
            session_id=sid,
            resolved_lead_id=lead_id,
            channel=channel,
            mode="ai",
            inbound_text="hi again, following up on the quote",
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_prior_summaries_are_injected_untrusted_for_a_returning_lead():
    admin, iid, lead = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed(admin, iid)
    await _seed_prior_summary(admin, iid, lead, "Intent: patio quote | Turns: 4")

    model = CapturingModel()
    await _run_turn(admin, iid, channel="sms", lead_id=lead, model=model)

    assert model.calls, "model was never called"
    first_msg = model.calls[0][0]
    # Injected BEFORE this session's history, as untrusted data (§3.4.14 Defense 2).
    assert first_msg["role"] == "user"
    assert "Prior conversation summaries:" in first_msg["content"]
    assert "patio quote" in first_msg["content"]
    assert "UNTRUSTED" in first_msg["content"].upper()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_widget_turns_never_receive_cross_session_memory():
    """HARD RULE §3.4.9: a weak widget token never inherits history — even when a
    lead id is resolved mid-conversation, the widget channel carries no memory."""
    admin, iid, lead = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed(admin, iid)
    await _seed_prior_summary(admin, iid, lead, "Intent: patio quote | Turns: 4")

    model = CapturingModel()
    await _run_turn(admin, iid, channel="widget", lead_id=lead, model=model)

    assert model.calls
    joined = " ".join(m["content"] for m in model.calls[0])
    assert "Prior conversation summaries:" not in joined


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_no_lead_means_no_memory_injection():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    await _seed(admin, iid)
    await _seed_prior_summary(admin, iid, uuid.uuid4(), "someone else's summary")

    model = CapturingModel()
    await _run_turn(admin, iid, channel="sms", lead_id=None, model=model)

    assert model.calls
    joined = " ".join(m["content"] for m in model.calls[0])
    assert "Prior conversation summaries:" not in joined
