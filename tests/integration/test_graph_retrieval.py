"""Graph store + hybrid retrieval integration tests (Architecture §3.2.1 — Path Locked).

Real Postgres recursive CTEs (no Neo4j/Memgraph — §3.2.1). Covers:
  * ingest extracts typed nodes + directed edges (round-trip through the real service),
  * recursive-CTE multi-hop traversal returns correct relationship facts,
  * traversal is tenant-isolated (a second tenant's graph is invisible — RLS),
  * the hybrid retriever engages graph ONLY on structured-filter intent (the §3.2.1 gate),
  * source soft-delete cascades the graph (§3.2.1 rule 5),
  * the correctness boundary: retrieval returns ONLY ingested knowledge (§3.2 ~line 291).
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.knowledge import graph, service
from app.knowledge.intent import QueryIntent, classify_intent
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.knowledge import KnowledgeEdge, KnowledgeEntity
from app.runtime.knowledge_retrieval import hybrid_retrieve
from sqlalchemy import func, select

pytestmark = [pytest.mark.asyncio]

# Recruiting-vertical graph: Platform HAS Senior Engineer REQUIRES Python (2-hop chain).
GRAPH_TEXT = """
Platform (Department)
Senior Engineer (Role): level=senior
Python (Skill)
Kubernetes (Skill)
Platform -HAS-> Senior Engineer
Senior Engineer -REQUIRES-> Python
Senior Engineer -REQUIRES-> Kubernetes
"""


async def _seed_instance(admin_id: uuid.UUID) -> uuid.UUID:
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
async def test_ingest_extracts_nodes_and_edges():
    admin = uuid.uuid4()
    iid = await _seed_instance(admin)
    async with tenant_session(admin) as s:
        await service.ingest_text(
            s, admin_id=admin, instance_id=iid, name="org.txt", text=GRAPH_TEXT
        )
    async with tenant_session(admin) as s:
        ents = (await s.execute(select(func.count()).select_from(KnowledgeEntity))).scalar_one()
        edges = (await s.execute(select(func.count()).select_from(KnowledgeEdge))).scalar_one()
        assert ents == 4  # Platform, Senior Engineer, Python, Kubernetes
        assert edges == 3  # HAS + 2× REQUIRES


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_recursive_cte_multi_hop_traversal():
    admin = uuid.uuid4()
    iid = await _seed_instance(admin)
    async with tenant_session(admin) as s:
        await service.ingest_text(
            s, admin_id=admin, instance_id=iid, name="org.txt", text=GRAPH_TEXT
        )
    async with tenant_session(admin) as s:
        # Seed from "Platform" → should reach Senior Engineer (1 hop) AND Python/Kubernetes
        # (2 hops) via the recursive CTE.
        facts = await graph.graph_facts_for_terms(
            s, admin_id=admin, instance_id=iid, terms=["Platform"]
        )
    joined = " ".join(facts).lower()
    assert "senior engineer" in joined
    assert "python" in joined  # reached only by walking 2 hops
    assert "kubernetes" in joined


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_traversal_is_tenant_isolated():
    a, b = uuid.uuid4(), uuid.uuid4()
    iid_a = await _seed_instance(a)
    await _seed_instance(b)
    async with tenant_session(a) as s:
        await service.ingest_text(s, admin_id=a, instance_id=iid_a, name="org.txt", text=GRAPH_TEXT)
    # Tenant B traverses with tenant A's instance id + seed term — RLS must hide A's graph.
    async with tenant_session(b) as s:
        facts = await graph.graph_facts_for_terms(
            s, admin_id=b, instance_id=iid_a, terms=["Platform"]
        )
    assert facts == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_hybrid_engages_graph_only_on_structured_intent():
    admin = uuid.uuid4()
    iid = await _seed_instance(admin)
    async with tenant_session(admin) as s:
        await service.ingest_text(
            s, admin_id=admin, instance_id=iid, name="org.txt", text=GRAPH_TEXT
        )
    # Structured-filter query → graph facts present in the fused result.
    async with tenant_session(admin) as s:
        structured = await hybrid_retrieve(
            s, admin_id=admin, instance_id=iid, query="Which roles require Python?"
        )
    assert any("require" in r.lower() for r in structured)

    # Pure-semantic query → graph NOT engaged. classify_intent is the gate; assert it
    # routes semantic AND that the fused output contains no graph relationship fact
    # (graph facts end with "(<type>)." and contain a lowercased relationship phrase).
    assert classify_intent("Tell me about the team") is QueryIntent.semantic
    async with tenant_session(admin) as s:
        semantic = await hybrid_retrieve(
            s, admin_id=admin, instance_id=iid, query="Tell me about the team"
        )
    assert not any(r.endswith(").") and " has " in r.lower() for r in semantic)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_pure_semantic_does_not_call_graph(monkeypatch):
    # Hard assertion of the §3.2.1 no-noise rule: on a semantic query the graph traversal
    # function must NEVER be invoked.
    admin = uuid.uuid4()
    iid = await _seed_instance(admin)
    async with tenant_session(admin) as s:
        await service.ingest_text(
            s, admin_id=admin, instance_id=iid, name="org.txt", text=GRAPH_TEXT
        )

    called = {"graph": False}

    async def _spy(*args, **kwargs):
        called["graph"] = True
        return []

    monkeypatch.setattr("app.runtime.knowledge_retrieval.graph.graph_facts_for_terms", _spy)
    async with tenant_session(admin) as s:
        await hybrid_retrieve(s, admin_id=admin, instance_id=iid, query="What are your hours?")
    assert called["graph"] is False, "graph must not be consulted for a pure-semantic query"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_hybrid_fusion_orders_graph_first_then_vector():
    # Deterministic fusion: structured query → graph facts precede vector chunks, deduped.
    admin = uuid.uuid4()
    iid = await _seed_instance(admin)
    async with tenant_session(admin) as s:
        await service.ingest_text(
            s, admin_id=admin, instance_id=iid, name="org.txt", text=GRAPH_TEXT
        )
    async with tenant_session(admin) as s:
        fused = await hybrid_retrieve(
            s, admin_id=admin, instance_id=iid, query="Which roles require Python?", k=10
        )
    # The first item is a graph relationship fact (ends with a typed parenthetical).
    assert fused
    assert fused[0].endswith(").")
    # No duplicates in the fused output.
    assert len(fused) == len(set(fused))


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_source_delete_cascades_graph():
    admin = uuid.uuid4()
    iid = await _seed_instance(admin)
    async with tenant_session(admin) as s:
        src = await service.ingest_text(
            s, admin_id=admin, instance_id=iid, name="org.txt", text=GRAPH_TEXT
        )
        src_id = src.id
    async with tenant_session(admin) as s:
        await service.delete_source(s, admin, src_id)
    async with tenant_session(admin) as s:
        ents = (await s.execute(select(func.count()).select_from(KnowledgeEntity))).scalar_one()
        edges = (await s.execute(select(func.count()).select_from(KnowledgeEdge))).scalar_one()
        assert ents == 0
        assert edges == 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_correctness_boundary_only_ingested_knowledge():
    # §3.2 correctness boundary: the graph returns ONLY what the admin ingested, never a
    # fabricated/live record. A term that was never ingested yields no facts.
    admin = uuid.uuid4()
    iid = await _seed_instance(admin)
    async with tenant_session(admin) as s:
        await service.ingest_text(
            s, admin_id=admin, instance_id=iid, name="org.txt", text=GRAPH_TEXT
        )
    async with tenant_session(admin) as s:
        facts = await graph.graph_facts_for_terms(
            s, admin_id=admin, instance_id=iid, terms=["NonexistentEntity"]
        )
    assert facts == []
