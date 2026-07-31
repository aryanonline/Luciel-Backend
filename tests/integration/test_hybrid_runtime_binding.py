"""Runtime binding of the Arc 16 production hybrid retriever (Architecture §3.2.1 / §3.4.1).

Closes the Arc 14/14b "empty retriever (Arc 11/16)" deferred stubs: the orchestrator and the
voice pipeline now construct the REAL hybrid retriever (app/runtime/knowledge_retrieval.py).
These tests prove:
  * an orchestrator loop driven by the REAL hybrid retriever over seeded knowledge returns
    content grounded in the ingested chunks (§3.4.13 grounding floor 0.50), and
  * the voice pipeline no longer binds an empty stub — its retriever returns ingested content.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.knowledge import service
from app.models.conversation import Session as SessionModel
from app.models.instance import Account, AccountState, Instance, LucielState
from app.runtime.grounding import GROUNDING_FLOOR
from app.runtime.knowledge_retrieval import make_retriever
from app.runtime.llm_router import LLMRouter
from app.runtime.orchestrator import Orchestrator
from app.runtime.testing import EchoModel

pytestmark = [pytest.mark.asyncio]

KNOWLEDGE = """
Our refund policy allows returns within thirty days of purchase for a full refund.
Refund (Policy): window=30 days
"""


async def _seed(admin_id: uuid.UUID) -> uuid.UUID:
    async with tenant_session(admin_id) as s:
        s.add(
            Account(
                id=admin_id,
                admin_id=admin_id,
                user_id=admin_id,
                email=f"{admin_id}@x.com",
                email_lower=f"{admin_id}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        iid = uuid.uuid4()
        s.add(
            Instance(
                id=iid,
                admin_id=admin_id,
                name="L",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
        return iid


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_orchestrator_loop_with_real_retriever_returns_grounded_content():
    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        await service.ingest_text(s, admin_id=admin, instance_id=iid, name="kb.txt", text=KNOWLEDGE)
    async with tenant_session(admin) as s:
        sid = uuid.uuid4()
        s.add(SessionModel(id=sid, admin_id=admin, instance_id=iid, channel="widget", mode="ai"))
        await s.flush()
        # The REAL production retriever (vector + intent-gated graph), bound exactly as the
        # widget/voice construction sites bind it.
        orch = Orchestrator(
            session=s,
            router=LLMRouter(primary=EchoModel(confidence=0.9), fallback=EchoModel()),
            retriever=make_retriever(s, admin_id=admin, instance_id=iid),
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
            inbound_text="What is your refund policy?",
        )
    # EchoModel echoes the retrieved Knowledge block, so the answer is grounded in the
    # ingested chunk above the 0.50 floor (§3.4.13) — the retriever is real, not the stub.
    assert result.grounding_score is not None
    assert result.grounding_score >= GROUNDING_FLOOR
    assert "refund" in result.reply_text.lower()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_make_retriever_returns_ingested_content_not_empty():
    # Direct proof the bound retriever is not the Arc 14b empty stub: it returns ingested
    # knowledge for a matching query.
    admin = uuid.uuid4()
    iid = await _seed(admin)
    async with tenant_session(admin) as s:
        await service.ingest_text(s, admin_id=admin, instance_id=iid, name="kb.txt", text=KNOWLEDGE)
    async with tenant_session(admin) as s:
        retriever = make_retriever(s, admin_id=admin, instance_id=iid)
        hits = await retriever("What is your refund policy?")
    assert hits, "bound retriever must return ingested content (stub is closed)"
    assert any("refund" in h.lower() for h in hits)


async def test_voice_pipeline_binds_real_retriever_not_stub():
    # Static guard: the voice pipeline imports make_retriever and no longer defines a local
    # empty `_retriever` stub (§3.2.1 binding). Reads the source to assert the stub is gone.
    import inspect

    from app.runtime import voice_pipeline

    src = inspect.getsource(voice_pipeline)
    assert "make_retriever(" in src
    assert "return []" not in src, "voice pipeline still contains an empty retriever stub"
