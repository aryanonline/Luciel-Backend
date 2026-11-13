"""Retrieval ranks on vector similarity, so real embeddings actually buy relevance.

The retrieval mechanics (HNSW + cosine over ``vector(1536)``) were always real; what was
missing was semantics in the vectors. This test pins the property that makes the swap
worthwhile: given an embedder that encodes meaning, a paraphrased question with ZERO
token overlap with the relevant document still retrieves it ahead of an irrelevant one.

The embedder here is a tiny deterministic concept mapper rather than a network call —
the point is that ``retrieve`` propagates whatever semantics the provider supplies
through pgvector, which is exactly what ``HashingEmbedder`` could not supply.
"""

from __future__ import annotations

import math
import uuid

import pytest
from app.db.session import tenant_session
from app.knowledge import pgvector, service
from app.models.instance import Account, AccountState, Instance, LucielState
from app.models.knowledge import KnowledgeChunk, KnowledgeSource
from app.runtime.embeddings import EMBED_DIM

# Deliberately disjoint vocabularies per concept: the query wording shares no token with
# the document wording, so a lexical matcher has nothing to go on.
_CONCEPTS: dict[str, set[str]] = {
    "hours": {"open", "9-5", "weekdays", "time", "close", "closing"},
    "refunds": {"refund", "refunds", "returned", "money"},
}


class ConceptEmbedder:
    """Deterministic stand-in for a real embedding model: maps text to concept axes."""

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        tokens = {t.strip(".,?!:;").lower() for t in text.split()}
        vec = [0.0] * EMBED_DIM
        for axis, (_, vocab) in enumerate(sorted(_CONCEPTS.items())):
            if tokens & vocab:
                vec[axis] = 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0.0:
            vec[len(_CONCEPTS)] = 1.0  # off-concept text: its own orthogonal axis
            norm = 1.0
        return [x / norm for x in vec]


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
async def test_paraphrased_query_ranks_the_relevant_chunk_first(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(service, "get_embedder", lambda *a, **k: ConceptEmbedder())
    admin = uuid.uuid4()
    iid = await _seed(admin)
    relevant = "We are open 9-5 on weekdays."
    irrelevant = "Anything returned in its packaging gets a full refund."

    async with tenant_session(admin) as s:
        await service.ingest_text(
            s, admin_id=admin, instance_id=iid, name="hours.txt", text=relevant
        )
        await service.ingest_text(
            s, admin_id=admin, instance_id=iid, name="refunds.txt", text=irrelevant
        )
        results = await service.retrieve(
            s,
            admin_id=admin,
            instance_id=iid,
            # No token in this question appears in either document.
            query="What time do you close?",
            k=2,
        )

    assert [c.text for c, _ in results][0] == relevant
    top_distance, next_distance = results[0][1], results[1][1]
    assert top_distance < next_distance, "ranking must come from cosine distance, not insert order"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_small_tenant_beside_a_large_one_still_gets_k(monkeypatch: pytest.MonkeyPatch):
    """Round 6 WP-G (F044): one global HNSW index, tenant filter AFTER the walk. With the
    default ef_search a 3-chunk tenant whose vectors tie with 300 of a neighbour's would
    get the neighbour's 40 candidates, filtered to nothing. ``retrieve`` sets ef_search
    and (pgvector 0.8+) iterative scans per transaction, so it still returns k."""
    monkeypatch.setattr(service, "get_embedder", lambda *a, **k: ConceptEmbedder())
    pgvector.reset_cache()
    big, small = uuid.uuid4(), uuid.uuid4()
    big_iid, small_iid = await _seed(big), await _seed(small)
    refunds_vec = (await ConceptEmbedder().embed(["refund money"]))[0]

    async with tenant_session(big) as s:
        src = KnowledgeSource(
            id=uuid.uuid4(),
            admin_id=big,
            instance_id=big_iid,
            name="big.txt",
            origin="upload",
            ingestion_status="ready",
            size_bytes=1,
        )
        s.add(src)
        for i in range(300):
            s.add(
                KnowledgeChunk(
                    id=uuid.uuid4(),
                    admin_id=big,
                    instance_id=big_iid,
                    source_id=src.id,
                    ordinal=i,
                    text=f"refund policy paragraph {i}",
                    embedding=refunds_vec,
                    embedding_model="concept",
                )
            )
    async with tenant_session(small) as s:
        for i in range(3):
            await service.ingest_text(
                s,
                admin_id=small,
                instance_id=small_iid,
                name=f"refunds-{i}.txt",
                text=f"Refunds: money is returned within {i + 7} days.",
            )
        version = await pgvector.installed_version(s)
        assert pgvector.supports_iterative_scan(version), version
        results = await service.retrieve(
            s, admin_id=small, instance_id=small_iid, query="refund my money", k=3
        )
    assert len(results) == 3
    assert all(c.admin_id == small for c, _ in results)
