"""Knowledge ingestion, retrieval, quota, and raw-view operations (Architecture §3.2).

Ingestion pipeline: source row (status pending) → chunk → embed → store chunks →
status ready. Retrieval: filter by (admin_id, instance_id, status=ready) then HNSW
ANN over the embedding, returning top-k chunks for the runtime. Quota: 5 GB total
/ 50 MB per file enforced at the API boundary (Vision §3.3).

All operations run inside a tenant_session so the three-layer isolation applies;
RLS guarantees a tenant only ever ingests/retrieves its own knowledge.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.knowledge.chunking import chunk_text
from app.knowledge.embeddings import get_embedder
from app.knowledge.graph_extract import GraphExtractor, get_extractor
from app.models.audit import ActorType
from app.models.knowledge import (
    KnowledgeChunk,
    KnowledgeEdge,
    KnowledgeEntity,
    KnowledgeSource,
)

TOTAL_QUOTA_BYTES = 5 * 1000 * 1000 * 1000  # 5 GB (Vision §7)
PER_FILE_MAX_BYTES = 50 * 1000 * 1000  # 50 MB


class QuotaError(Exception):
    """Raised when an ingest would exceed the storage quota (→ 422 at the edge)."""


async def used_bytes(session: AsyncSession, admin_id: uuid.UUID) -> int:
    total = (
        await session.execute(
            select(func.coalesce(func.sum(KnowledgeSource.size_bytes), 0)).where(
                KnowledgeSource.admin_id == admin_id,
                KnowledgeSource.soft_deleted_at.is_(None),
            )
        )
    ).scalar_one()
    return int(total or 0)


async def quota(session: AsyncSession, admin_id: uuid.UUID) -> dict[str, int]:
    return {
        "usedBytes": await used_bytes(session, admin_id),
        "totalBytes": TOTAL_QUOTA_BYTES,
        "perFileMaxBytes": PER_FILE_MAX_BYTES,
    }


async def ingest_text(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    name: str,
    text: str,
    origin: str = "upload",
    size_bytes: int | None = None,
    actor_user_id: uuid.UUID | None = None,
    extractor: GraphExtractor | None = None,
) -> KnowledgeSource:
    """Parse → chunk → embed → store → extract graph. Returns the ready source row.

    The graph extractor is injected (defaults to the deterministic one) so the ingest
    path depends only on the GraphExtractor Protocol (§3.2.1 pluggable extractor)."""
    raw_size = size_bytes if size_bytes is not None else len(text.encode("utf-8"))
    if raw_size > PER_FILE_MAX_BYTES:
        raise QuotaError("per_file_limit_exceeded")
    if await used_bytes(session, admin_id) + raw_size > TOTAL_QUOTA_BYTES:
        raise QuotaError("total_quota_exceeded")

    source = KnowledgeSource(
        id=uuid.uuid4(),
        admin_id=admin_id,
        instance_id=instance_id,
        name=name,
        origin=origin,
        ingestion_status="pending",
        size_bytes=raw_size,
        source_origin=origin,
        last_updated_at=datetime.now(UTC),
    )
    session.add(source)
    await session.flush()

    chunks = chunk_text(text)
    if chunks:
        embedder = get_embedder()
        vectors = await embedder.embed(chunks)
        for ordinal, (chunk, vec) in enumerate(zip(chunks, vectors, strict=True)):
            session.add(
                KnowledgeChunk(
                    id=uuid.uuid4(),
                    admin_id=admin_id,
                    instance_id=instance_id,
                    source_id=source.id,
                    ordinal=ordinal,
                    text=chunk,
                    embedding=vec,
                )
            )
    # --- Graph extraction at ingest (§3.2.1) ----------------------------------- #
    # Typed nodes + directed edges are extracted from the SAME ingested text and stored
    # scoped by (admin_id, instance_id) under the same source_id, so they obey identical
    # tenant-isolation + cascade-delete rules as the chunks (§3.2.1 rule 5). The graph
    # operates ONLY over admin-ingested knowledge — never live records (§3.2 correctness
    # boundary): extraction reads `text`, the very content the admin gave us.
    entity_ids = await _store_graph(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        source_id=source.id,
        text=text,
        extractor=extractor or get_extractor(),
    )

    source.ingestion_status = "ready"
    await session.flush()
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        actor_user_id=actor_user_id,
        actor_type=ActorType.admin_user if actor_user_id else ActorType.platform,
        event_type="knowledge_source_edited",
        payload={
            "source_id": str(source.id),
            "action": "ingest",
            "chunks": len(chunks),
            "entities": len(entity_ids),
        },
    )
    return source


async def _store_graph(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    source_id: uuid.UUID,
    text: str,
    extractor: GraphExtractor,
) -> dict[str, uuid.UUID]:
    """Extract + persist typed nodes / directed edges for one source (§3.2.1).

    Returns the canonical-name → entity_id map. Edges are resolved against that map so
    src/dst reference the persisted entity rows; an endpoint with no node row was already
    materialized as an `unknown` entity by the extractor, so edges never dangle."""
    result = extractor.extract(text)
    id_by_canonical: dict[str, uuid.UUID] = {}
    for ent in result.entities:
        eid = uuid.uuid4()
        id_by_canonical[ent.canonical] = eid
        session.add(
            KnowledgeEntity(
                id=eid,
                admin_id=admin_id,
                instance_id=instance_id,
                source_id=source_id,
                entity_type=ent.entity_type,
                name=ent.name,
                canonical=ent.canonical,
                attributes=ent.attributes,
            )
        )
    from app.knowledge.graph_extract import canonicalize

    for edge in result.edges:
        src_id = id_by_canonical.get(canonicalize(edge.src_name))
        dst_id = id_by_canonical.get(canonicalize(edge.dst_name))
        if src_id is None or dst_id is None:
            continue  # unreachable: extractor materializes every endpoint as a node.
        session.add(
            KnowledgeEdge(
                id=uuid.uuid4(),
                admin_id=admin_id,
                instance_id=instance_id,
                source_id=source_id,
                src_entity_id=src_id,
                dst_entity_id=dst_id,
                relationship_type=edge.relationship_type,
                attributes=edge.attributes,
            )
        )
    await session.flush()
    return id_by_canonical


async def list_sources(session: AsyncSession, admin_id: uuid.UUID) -> list[KnowledgeSource]:
    return list(
        (
            await session.execute(
                select(KnowledgeSource)
                .where(
                    KnowledgeSource.admin_id == admin_id,
                    KnowledgeSource.soft_deleted_at.is_(None),
                )
                .order_by(KnowledgeSource.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


async def get_chunks(
    session: AsyncSession, admin_id: uuid.UUID, source_id: uuid.UUID, limit: int = 20
) -> list[KnowledgeChunk]:
    return list(
        (
            await session.execute(
                select(KnowledgeChunk)
                .where(
                    KnowledgeChunk.admin_id == admin_id,
                    KnowledgeChunk.source_id == source_id,
                )
                .order_by(KnowledgeChunk.ordinal.asc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def view_chunks(
    session: AsyncSession,
    admin_id: uuid.UUID,
    source_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
    limit: int = 20,
) -> list[KnowledgeChunk]:
    """Return a source's chunks AND emit a ``knowledge_source_viewed`` audit event.

    Architecture §3.2.2 requires that view + edit + delete history of a knowledge
    source flows into ``admin_audit_log``; §5.1 registers ``knowledge_source_viewed``
    as a defined event. The chunk-preview surface is the "view" of a source, so the
    raw-view chunk read is the correct emit site (audit finding Axis4-F04). Only
    emits when the source actually exists for this tenant (no event for a 404).
    """
    source = (
        await session.execute(
            select(KnowledgeSource).where(
                KnowledgeSource.admin_id == admin_id, KnowledgeSource.id == source_id
            )
        )
    ).scalar_one_or_none()
    if source is None:
        return []
    chunks = await get_chunks(session, admin_id, source_id, limit=limit)
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=source.instance_id,
        actor_user_id=actor_user_id,
        actor_type=ActorType.admin_user if actor_user_id else ActorType.platform,
        event_type="knowledge_source_viewed",
        payload={"source_id": str(source_id), "chunks": len(chunks)},
    )
    return chunks


async def delete_source(
    session: AsyncSession,
    admin_id: uuid.UUID,
    source_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
) -> bool:
    """Soft-delete the source and hard-remove its chunks (Architecture §3.2.2).

    The source row keeps a soft_deleted_at tombstone (30-day grace pattern); its
    embeddings are removed so the runtime stops retrieving from it immediately.
    """
    source = (
        await session.execute(
            select(KnowledgeSource).where(
                KnowledgeSource.admin_id == admin_id, KnowledgeSource.id == source_id
            )
        )
    ).scalar_one_or_none()
    if source is None:
        return False
    source.soft_deleted_at = datetime.now(UTC)
    await session.execute(
        delete(KnowledgeChunk).where(
            KnowledgeChunk.admin_id == admin_id, KnowledgeChunk.source_id == source_id
        )
    )
    # The source's graph nodes/edges cascade exactly like its chunks (§3.2.1 rule 5 /
    # §3.2.2): once the source is gone the runtime must stop traversing its graph too.
    # Delete edges first (they reference entities) then the entities themselves.
    await session.execute(
        delete(KnowledgeEdge).where(
            KnowledgeEdge.admin_id == admin_id, KnowledgeEdge.source_id == source_id
        )
    )
    await session.execute(
        delete(KnowledgeEntity).where(
            KnowledgeEntity.admin_id == admin_id, KnowledgeEntity.source_id == source_id
        )
    )
    await session.flush()
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=source.instance_id,
        actor_user_id=actor_user_id,
        actor_type=ActorType.admin_user if actor_user_id else ActorType.platform,
        event_type="knowledge_source_deleted",
        payload={"source_id": str(source_id)},
    )
    return True


async def rename_source(
    session: AsyncSession,
    admin_id: uuid.UUID,
    source_id: uuid.UUID,
    new_name: str,
    actor_user_id: uuid.UUID | None = None,
) -> KnowledgeSource | None:
    """Rename affordance (Architecture §3.2.2: "rename source, re-trigger ingestion,
    or replace the file"). Chunks/embeddings are untouched — only the display name
    changes. Returns None if the source does not exist for this tenant."""
    source = (
        await session.execute(
            select(KnowledgeSource).where(
                KnowledgeSource.admin_id == admin_id,
                KnowledgeSource.id == source_id,
                KnowledgeSource.soft_deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if source is None:
        return None
    old_name = source.name
    source.name = new_name
    source.last_updated_at = datetime.now(UTC)
    await session.flush()
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=source.instance_id,
        actor_user_id=actor_user_id,
        actor_type=ActorType.admin_user if actor_user_id else ActorType.platform,
        event_type="knowledge_source_edited",
        payload={
            "source_id": str(source_id),
            "action": "rename",
            "old_name": old_name,
            "new_name": new_name,
        },
    )
    return source


async def replace_source(
    session: AsyncSession,
    admin_id: uuid.UUID,
    source_id: uuid.UUID,
    *,
    text: str,
    size_bytes: int,
    actor_user_id: uuid.UUID | None = None,
    extractor: GraphExtractor | None = None,
) -> KnowledgeSource | None:
    """Replace affordance (Architecture §3.2.2): re-ingest a NEW file into an
    EXISTING source row. The source id/name/place-in-list is preserved but its
    chunks, embeddings, and graph nodes/edges are fully replaced by the new
    content — this is the "corrected source is re-ingested, the chunks are
    updated" root-cause-fix flow (§5.2/§9). Returns None if the source does not
    exist for this tenant. Quota is re-checked against the NEW size, crediting
    back the old size first so a same-size or smaller replace never false-fails.
    """
    source = (
        await session.execute(
            select(KnowledgeSource).where(
                KnowledgeSource.admin_id == admin_id,
                KnowledgeSource.id == source_id,
                KnowledgeSource.soft_deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if source is None:
        return None

    old_size = source.size_bytes
    if size_bytes > PER_FILE_MAX_BYTES:
        raise QuotaError("per_file_limit_exceeded")
    used_without_old = await used_bytes(session, admin_id) - old_size
    if used_without_old + size_bytes > TOTAL_QUOTA_BYTES:
        raise QuotaError("total_quota_exceeded")

    # Wipe the old chunks + graph (identical cascade to delete_source, minus the
    # tombstone) before re-ingesting the new content under the SAME source id.
    await session.execute(
        delete(KnowledgeChunk).where(
            KnowledgeChunk.admin_id == admin_id, KnowledgeChunk.source_id == source_id
        )
    )
    await session.execute(
        delete(KnowledgeEdge).where(
            KnowledgeEdge.admin_id == admin_id, KnowledgeEdge.source_id == source_id
        )
    )
    await session.execute(
        delete(KnowledgeEntity).where(
            KnowledgeEntity.admin_id == admin_id, KnowledgeEntity.source_id == source_id
        )
    )

    chunks = chunk_text(text)
    if chunks:
        embedder = get_embedder()
        vectors = await embedder.embed(chunks)
        for ordinal, (chunk, vec) in enumerate(zip(chunks, vectors, strict=True)):
            session.add(
                KnowledgeChunk(
                    id=uuid.uuid4(),
                    admin_id=admin_id,
                    instance_id=source.instance_id,
                    source_id=source.id,
                    ordinal=ordinal,
                    text=chunk,
                    embedding=vec,
                )
            )
    await _store_graph(
        session,
        admin_id=admin_id,
        instance_id=source.instance_id,
        source_id=source.id,
        text=text,
        extractor=extractor or get_extractor(),
    )

    source.size_bytes = size_bytes
    source.ingestion_status = "ready"
    source.last_updated_at = datetime.now(UTC)
    await session.flush()
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=source.instance_id,
        actor_user_id=actor_user_id,
        actor_type=ActorType.admin_user if actor_user_id else ActorType.platform,
        event_type="knowledge_source_edited",
        payload={"source_id": str(source_id), "action": "replace", "chunks": len(chunks)},
    )
    return source


# Retrieval re-rank tuning (Architecture §3.2 retrieval flow, step 3:
# "Re-rank by recency and chunk-ordinal proximity"). The ANN pass (step 2) is the
# dominant relevance signal; re-rank is a TIE-BREAK within a small cosine band so it
# cannot reorder semantically-distinct results (which would regress recall). Two
# candidates are "close" if their cosine distances differ by <= _RERANK_BAND; within a
# band the more recently updated source and the lower-ordinal chunk are preferred.
_RERANK_BAND = 0.05
_RERANK_OVERFETCH = 4  # pull a wider candidate set than k so the re-rank has material


async def retrieve(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    query: str,
    k: int = 5,
) -> list[tuple[KnowledgeChunk, float]]:
    """HNSW ANN retrieval over ready chunks. Returns (chunk, cosine_distance) pairs.

    Filters to (admin_id, instance_id) and only sources whose ingestion is ready
    and not soft-deleted (Architecture §3.2 retrieval flow). RLS is the backstop.

    Implements the §3.2 retrieval flow in full:
      step 2 — HNSW ANN over the embedding (ordered by cosine distance);
      step 3 — re-rank the top candidates by recency + chunk-ordinal proximity as a
               tie-break within a narrow cosine band, then truncate to k.
    """
    embedder = get_embedder()
    qvec = (await embedder.embed([query]))[0]
    distance = KnowledgeChunk.embedding.cosine_distance(qvec)
    # Over-fetch: the ANN gives us more candidates than k so step-3 re-ranking has
    # material to reorder within the cosine band before we truncate to k.
    rows = (
        await session.execute(
            select(
                KnowledgeChunk,
                distance.label("distance"),
                KnowledgeSource.last_updated_at.label("src_updated_at"),
            )
            .join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id)
            .where(
                KnowledgeChunk.admin_id == admin_id,
                KnowledgeChunk.instance_id == instance_id,
                KnowledgeSource.ingestion_status == "ready",
                KnowledgeSource.soft_deleted_at.is_(None),
            )
            .order_by(distance.asc())
            .limit(max(k, k + _RERANK_OVERFETCH))
        )
    ).all()
    candidates = [
        (row[0], float(row[1]), row[2]) for row in rows
    ]  # (chunk, cosine_distance, source_last_updated_at)
    reranked = _rerank(candidates)
    return [(chunk, dist) for chunk, dist, _ in reranked[:k]]


def _rerank(
    candidates: list[tuple[KnowledgeChunk, float, datetime | None]],
) -> list[tuple[KnowledgeChunk, float, datetime | None]]:
    """Step-3 re-rank: stable tie-break by recency + chunk-ordinal proximity (§3.2).

    Cosine distance stays the primary key, so a clearly-more-relevant chunk is never
    demoted. Only within a narrow distance band (<= _RERANK_BAND) do we prefer the
    fresher source and then the lower-ordinal chunk. We quantise the distance into
    bands and sort by (band, -recency, ordinal, distance) so the reorder is bounded
    and deterministic.
    """
    if len(candidates) <= 1:
        return candidates
    epoch = datetime(1970, 1, 1, tzinfo=UTC)

    def sort_key(
        item: tuple[KnowledgeChunk, float, datetime | None],
    ) -> tuple[int, float, int, float]:
        chunk, dist, updated_at = item
        band = int(dist / _RERANK_BAND)  # candidates in the same band are "close"
        ts = updated_at or epoch
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        recency = ts.timestamp()
        # Within a band: newer source first (-recency), then lower ordinal, then the
        # raw distance as the final, fully-deterministic tie-break.
        return (band, -recency, chunk.ordinal, dist)

    return sorted(candidates, key=sort_key)
