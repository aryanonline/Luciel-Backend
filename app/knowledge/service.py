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
from app.knowledge.graph_extract import GraphExtractor, get_extractor
from app.models.audit import ActorType
from app.models.knowledge import (
    KnowledgeChunk,
    KnowledgeEdge,
    KnowledgeEntity,
    KnowledgeSource,
)
from app.runtime.embeddings import get_embedder

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
    """
    embedder = get_embedder()
    qvec = (await embedder.embed([query]))[0]
    distance = KnowledgeChunk.embedding.cosine_distance(qvec)
    rows = (
        await session.execute(
            select(KnowledgeChunk, distance.label("distance"))
            .join(KnowledgeSource, KnowledgeSource.id == KnowledgeChunk.source_id)
            .where(
                KnowledgeChunk.admin_id == admin_id,
                KnowledgeChunk.instance_id == instance_id,
                KnowledgeSource.ingestion_status == "ready",
                KnowledgeSource.soft_deleted_at.is_(None),
            )
            .order_by(distance.asc())
            .limit(k)
        )
    ).all()
    return [(row[0], float(row[1])) for row in rows]
