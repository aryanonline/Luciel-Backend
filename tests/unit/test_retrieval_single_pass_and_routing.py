"""Retrieval runs once per turn and the fast lane is real (2026-09-05 audit, cost).

The orchestrator embedded the visitor's question twice and ran two ANN searches per
turn — once for the prompt, once for attribution. The bundle does both from one
pass. In live mode a chunk beyond the relevance floor is dropped before injection.
And the bound Haiku-class provider was dead weight: no call site ever passed the
gate its three hints, so ``routing_hints`` now marks the honest fast lane — social
turns with nothing to ground and nothing for a tool to do.
"""

from __future__ import annotations

import uuid

import pytest
from app.config import get_settings
from app.knowledge import service
from app.runtime import knowledge_retrieval
from app.runtime.llm_router import should_route_fast
from app.runtime.orchestrator import routing_hints


class _Chunk:
    def __init__(self, source_id: uuid.UUID, text: str) -> None:
        self.source_id = source_id
        self.text = text


@pytest.mark.asyncio
@pytest.mark.usefixtures("db_engine_reset")
async def test_the_bundle_embeds_and_searches_once_and_returns_both_views(monkeypatch):
    calls = {"retrieve": 0}
    sid = uuid.uuid4()

    async def fake_retrieve(session, *, admin_id, instance_id, query, k=5):
        calls["retrieve"] += 1
        return [(_Chunk(sid, "Decks from $40/sqft"), 0.12), (_Chunk(sid, "Open 9-5"), 0.31)]

    async def fake_sources(session, *, admin_id, instance_id, source_ids):
        return {sid: "Price list"}

    monkeypatch.setattr(service, "retrieve", fake_retrieve)
    monkeypatch.setattr(service, "sources_by_id", fake_sources)
    texts, attributed = await knowledge_retrieval.hybrid_retrieve_bundle(
        None, admin_id=uuid.uuid4(), instance_id=uuid.uuid4(), query="how much are decks?"
    )
    assert calls["retrieve"] == 1
    assert texts == ["Decks from $40/sqft", "Open 9-5"]
    assert [a["source_name"] for a in attributed] == ["Price list", "Price list"]
    assert [a["text"] for a in attributed] == texts


@pytest.mark.asyncio
@pytest.mark.usefixtures("db_engine_reset")
async def test_the_relevance_floor_drops_unrelated_chunks_in_live_mode_only(monkeypatch):
    sid = uuid.uuid4()

    async def fake_retrieve(session, *, admin_id, instance_id, query, k=5):
        return [(_Chunk(sid, "relevant"), 0.2), (_Chunk(sid, "noise"), 0.9)]

    async def fake_sources(session, *, admin_id, instance_id, source_ids):
        return {sid: "Doc"}

    monkeypatch.setattr(service, "retrieve", fake_retrieve)
    monkeypatch.setattr(service, "sources_by_id", fake_sources)
    monkeypatch.setattr(get_settings(), "knowledge_relevance_max_distance", 0.6, raising=False)

    monkeypatch.setattr(knowledge_retrieval, "_relevance_floor_active", lambda: True)
    texts, attributed = await knowledge_retrieval.hybrid_retrieve_bundle(
        None, admin_id=uuid.uuid4(), instance_id=uuid.uuid4(), query="q"
    )
    assert texts == ["relevant"] and [a["text"] for a in attributed] == ["relevant"]

    # Mock posture: hashed vectors carry no distance meaning — nothing is dropped.
    monkeypatch.setattr(knowledge_retrieval, "_relevance_floor_active", lambda: False)
    texts, _ = await knowledge_retrieval.hybrid_retrieve_bundle(
        None, admin_id=uuid.uuid4(), instance_id=uuid.uuid4(), query="q"
    )
    assert texts == ["relevant", "noise"]


def test_the_floor_is_live_only_by_construction():
    assert get_settings().integration_mode != "live"
    assert knowledge_retrieval._relevance_floor_active() is False


# --- fast routing hints ------------------------------------------------------------- #
TOOLS = [{"name": "book_appointment", "input_schema": {}}]


def test_a_social_turn_takes_the_fast_lane_even_when_tools_exist():
    hints = routing_hints(
        "thanks, that's all!", "system", [{"role": "user", "content": "hi"}], TOOLS
    )
    assert hints["requires_tools"] is False
    assert hints["complexity"] < 0.5
    assert should_route_fast(**hints) is True


def test_a_real_question_keeps_the_primary_model():
    hints = routing_hints("can I book Tuesday at 10 and what does it cost?", "system", [], TOOLS)
    assert hints["requires_tools"] is True
    assert hints["complexity"] == 1.0
    assert should_route_fast(**hints) is False


def test_a_long_context_never_routes_fast_even_for_a_social_turn():
    from app.runtime.llm_router import CONTEXT_TOKEN_BUDGET_FAST

    big = [{"role": "user", "content": "x" * (CONTEXT_TOKEN_BUDGET_FAST * 4 + 100)}]
    hints = routing_hints("thanks!", "system", big, None)
    assert hints["context_tokens"] > CONTEXT_TOKEN_BUDGET_FAST
    assert should_route_fast(**hints) is False
