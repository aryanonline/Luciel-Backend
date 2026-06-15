"""Hybrid vector + graph retrieval (DOCTRINE ANCHOR — Architecture §3.2 / §3.2.1).

Vector (pgvector HNSW) lands in Arc 11; graph + hybrid in Arc 16. The graph is
invoked only on structured-filter-intent queries; pure-semantic queries go to
vector only (Locked Decision §7 #6).

This module binds the Arc 16 production retriever behind the orchestrator's injected
seam ``Retriever = Callable[[str], Awaitable[list[str]]]`` (app/runtime/orchestrator.py),
closing the Arc 14/14b "empty retriever (Arc 11/16)" deferred stubs in chat_widget.py and
voice_pipeline.py. The seam stays injectable so tests can still pass a fake retriever.

Routing (§3.2.1 retrieval router rule):
  * classify the query intent (app/knowledge/intent.py).
  * semantic            → VECTOR ONLY. The graph is NOT consulted (mixing it in adds
                          noise without accuracy benefit — §3.2.1).
  * structured_filter   → vector + graph, FUSED.

Fusion (documented, deterministic): a simple priority concatenation, NOT reciprocal-rank
interleaving. Graph facts are exact, structured relationship statements that directly
answer a structured-filter query, so they are placed FIRST; the vector chunks follow as
semantic context. Within each source the existing order is preserved (graph: stable CTE
ordering; vector: ascending cosine distance). Duplicates are dropped. This keeps fusion
trivial to reason about and test (§3.2.1 "keep it simple"), and the combined list is capped
at ``k`` so downstream prompt size stays bounded.

Correctness boundary (§3.2 ~line 291): BOTH stores return ONLY admin-ingested knowledge,
framed as "based on the information you gave me". Live exact-record lookups are a TOOL job,
never the knowledge stores — this retriever therefore never calls a live data source.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge import graph, service
from app.knowledge.intent import QueryIntent, classify_intent, seed_terms

# Matches app/runtime/orchestrator.py Retriever; re-declared here to avoid a runtime import
# cycle (orchestrator imports nothing from this module — binding happens at the call sites).
Retriever = Callable[[str], Awaitable[list[str]]]

DEFAULT_K = 5


async def hybrid_retrieve(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    query: str,
    k: int = DEFAULT_K,
) -> list[str]:
    """Intent-gated hybrid retrieval → ordered list of retrieved text snippets (§3.2.1).

    Always runs vector retrieval; engages graph traversal ONLY for structured-filter-intent
    queries (§3.2.1). Returns plain strings (the orchestrator wraps each as untrusted before
    the model — §3.4.14)."""
    intent = classify_intent(query)

    graph_facts: list[str] = []
    if intent is QueryIntent.structured_filter_intent:
        # Relationship traversal / multi-attribute intersection / membership (§3.2.1):
        # seed the recursive-CTE walk from the query's candidate entity terms. RLS scopes
        # this to the tenant; empty when no ingested entity matches (no fabrication, §3.2).
        graph_facts = await graph.graph_facts_for_terms(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            terms=seed_terms(query),
        )

    vector_hits = await service.retrieve(
        session, admin_id=admin_id, instance_id=instance_id, query=query, k=k
    )
    vector_texts = [chunk.text for chunk, _distance in vector_hits]

    # Fusion: graph facts first (exact structured answers), then vector context; dedupe,
    # cap at k (§3.2.1 documented merge). For semantic intent graph_facts is empty, so this
    # collapses to vector-only — the §3.2.1 pure-semantic path.
    fused: list[str] = []
    seen: set[str] = set()
    for item in [*graph_facts, *vector_texts]:
        if item and item not in seen:
            seen.add(item)
            fused.append(item)
    return fused[:k]


def make_retriever(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    k: int = DEFAULT_K,
) -> Retriever:
    """Bind a tenant-scoped production hybrid retriever for the orchestrator seam (§3.2.1).

    The returned callable matches ``Retriever`` and closes over the per-request session +
    tenant scope, so the orchestrator/voice construction sites pass a real retriever instead
    of the Arc 14/14b empty stub while the seam stays injectable for tests."""

    async def _retrieve(query: str) -> list[str]:
        return await hybrid_retrieve(
            session, admin_id=admin_id, instance_id=instance_id, query=query, k=k
        )

    return _retrieve
