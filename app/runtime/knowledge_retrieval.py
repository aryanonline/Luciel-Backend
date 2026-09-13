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

import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.knowledge import graph, service
from app.knowledge.embeddings import EmbeddingError
from app.knowledge.intent import QueryIntent, classify_intent, seed_terms
from app.observability.metrics import emit_metric

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


#: ``(texts_for_the_model, attributed_chunks)`` from ONE vector query (cost: the
#: orchestrator used to embed the same question twice and run two ANN searches per turn).
RetrieverBundle = Callable[[str], Awaitable[tuple[list[str], list[dict[str, object]]]]]

#: The one-entry ``source_chunks`` value persisted when retrieval could not run AT
#: ALL (the embedding provider failed — 2026-09-06 E2E walk: OpenAI answered 429 at
#: quota and every answer became a hand-off). It is a third truth next to ``[]``
#: (retrieval ran, nothing matched) and ``None`` (attribution never captured): the
#: evidence endpoint maps it to ``retrieval_unavailable`` so the owner reads
#: "your knowledge couldn't be searched", never "no source backed this answer"
#: over a question their sources do answer.
RETRIEVAL_UNAVAILABLE_KEY = "unavailable"


def retrieval_unavailable_marker(reason: str) -> dict[str, object]:
    """The ``source_chunks`` entry for a turn whose retrieval never ran."""
    return {RETRIEVAL_UNAVAILABLE_KEY: reason[:120]}


def is_retrieval_unavailable(chunks: list[dict[str, object]] | None) -> bool:
    """True when a persisted ``source_chunks`` value is the marker, not real chunks."""
    return bool(chunks) and all(RETRIEVAL_UNAVAILABLE_KEY in c for c in chunks or [])


def _relevance_floor_active() -> bool:
    # Hashed mock vectors carry no distance meaning; the floor is a live-only rule.
    return get_settings().integration_mode == "live"


def apply_relevance_floor[Hit: tuple[Any, float | None]](
    hits: Sequence[Hit], *, ceiling: float, band: float
) -> list[Hit]:
    """Drop hits past the absolute distance ``ceiling`` and hits more than ``band``
    farther than the best hit (round 6 R6-2). Unscored hits are kept."""
    scored = [float(hit[1]) for hit in hits if hit[1] is not None]
    best = min(scored) if scored else None
    kept: list[Hit] = []
    for hit in hits:
        d = hit[1]
        if d is None:
            kept.append(hit)
            continue
        dist = float(d)
        if dist > ceiling:
            continue
        if best is not None and dist > best + band:
            continue
        kept.append(hit)
    return kept


async def hybrid_retrieve_bundle(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    query: str,
    k: int = DEFAULT_K,
) -> tuple[list[str], list[dict[str, object]]]:
    """One retrieval pass → (prompt snippets, real chunk attribution) (§3.2.1).

    The vector leg runs ONCE (one embedding, one ANN); graph facts join the prompt
    list for structured-filter intent exactly as ``hybrid_retrieve`` does. In live
    mode a chunk beyond the relevance floor (cosine distance above
    ``knowledge_relevance_max_distance``, or more than ``knowledge_relevance_band``
    farther than the best hit) is dropped before it can be injected — an unrelated
    chunk is noise the model may answer from, and an honest "no sources" beats a
    confident answer from the wrong page."""
    intent = classify_intent(query)
    graph_facts: list[str] = []
    if intent is QueryIntent.structured_filter_intent:
        graph_facts = await graph.graph_facts_for_terms(
            session, admin_id=admin_id, instance_id=instance_id, terms=seed_terms(query)
        )
    unavailable_reason: str | None = None
    try:
        vector_hits = await service.retrieve(
            session, admin_id=admin_id, instance_id=instance_id, query=query, k=k
        )
    except EmbeddingError as exc:
        # The embedding provider is down (2026-09-05 audit F103): the turn goes on with
        # NO knowledge — the grounding floor then hands the question to a person —
        # instead of a 500 that answers the visitor with nothing at all. It is also
        # a metric with an alarm behind it (2026-09-06 E2E walk): a provider at
        # quota turns EVERY answer into a hand-off, and only a warning line said so.
        _LOG.warning("knowledge_retrieval_degraded reason=%s", exc)
        unavailable_reason = str(exc)
        emit_metric("KnowledgeRetrievalDegraded", dimensions={"Reason": unavailable_reason})
        vector_hits = []
    if _relevance_floor_active():
        settings = get_settings()
        vector_hits = apply_relevance_floor(
            vector_hits,
            ceiling=settings.knowledge_relevance_max_distance,
            band=settings.knowledge_relevance_band,
        )
    attributed: list[dict[str, object]] = []
    if vector_hits:
        source_ids = {chunk.source_id for chunk, _d in vector_hits}
        sources = await service.sources_by_id(
            session, admin_id=admin_id, instance_id=instance_id, source_ids=source_ids
        )
        attributed = [
            {
                "source_id": str(chunk.source_id),
                "source_name": sources.get(chunk.source_id, "Knowledge source"),
                "text": chunk.text,
            }
            for chunk, _d in vector_hits
        ]
    if unavailable_reason is not None:
        # Persisted in place of chunks: the dashboard must not read this turn as
        # "retrieval ran and found nothing".
        attributed = [retrieval_unavailable_marker(unavailable_reason)]
    fused: list[str] = []
    seen: set[str] = set()
    for item in [*graph_facts, *[chunk.text for chunk, _d in vector_hits]]:
        if item and item not in seen:
            seen.add(item)
            fused.append(item)
    return fused[:k], attributed


def make_retriever_bundle(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    k: int = DEFAULT_K,
) -> RetrieverBundle:
    """Bind the single-pass retriever for a construction site (widget, channels)."""

    async def _bundle(query: str) -> tuple[list[str], list[dict[str, object]]]:
        return await hybrid_retrieve_bundle(
            session, admin_id=admin_id, instance_id=instance_id, query=query, k=k
        )

    return _bundle


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


# Harmony wave 2, item 2a (LIVE-VERIFIED): source_id/source_name are already on
# every KnowledgeChunk row the vector store returns (app/knowledge/service.py::
# retrieve), but hybrid_retrieve() above collapses them to plain text before the
# orchestrator ever sees them, so persist_turn had nothing real to attach as
# source_chunks -- the dashboard evidence endpoint always rendered an empty list,
# which the frontend then rendered as "no source backed this answer" even when
# retrieval genuinely ran and found KB matches. ChunkAttributor is an ADDITIVE
# seam (not a change to the existing Retriever contract, to avoid rewriting every
# call site/fake across chat_widget/runtime_entry/voice_pipeline/tests): it
# answers the SAME query and returns the vector hits' real source metadata so the
# orchestrator can persist real chunk refs on new turns whose retriever actually
# has them. Graph facts have no KnowledgeChunk row to attribute to, so this only
# ever covers the vector leg -- deliberately: a graph-only structured-filter turn
# still persists source_chunks=None (attribution_unavailable), never a fabricated
# source for a fact that has no chunk this maps back to.
_LOG = logging.getLogger(__name__)

ChunkAttributor = Callable[[str], Awaitable[list[dict[str, object]]]]


async def hybrid_retrieve_sources(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    query: str,
    k: int = DEFAULT_K,
) -> list[dict[str, object]]:
    """Real source attribution for the vector leg of hybrid_retrieve (§3.2.1/§3.4.13).

    Returns [{"source_id", "source_name", "text"}, ...] for the same vector hits
    hybrid_retrieve() would fold into its plain-text list, so callers that want to
    PERSIST attribution (not just prompt the model) have the real chunk refs
    available, without changing hybrid_retrieve's existing return type."""
    vector_hits = await service.retrieve(
        session, admin_id=admin_id, instance_id=instance_id, query=query, k=k
    )
    if not vector_hits:
        return []
    source_ids = {chunk.source_id for chunk, _distance in vector_hits}
    sources = await service.sources_by_id(
        session, admin_id=admin_id, instance_id=instance_id, source_ids=source_ids
    )
    return [
        {
            "source_id": str(chunk.source_id),
            "source_name": sources.get(chunk.source_id, "Knowledge source"),
            "text": chunk.text,
        }
        for chunk, _distance in vector_hits
    ]


def make_chunk_attributor(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    k: int = DEFAULT_K,
) -> ChunkAttributor:
    """Bind a tenant-scoped chunk attributor alongside make_retriever's retriever."""

    async def _attribute(query: str) -> list[dict[str, object]]:
        return await hybrid_retrieve_sources(
            session, admin_id=admin_id, instance_id=instance_id, query=query, k=k
        )

    return _attribute
