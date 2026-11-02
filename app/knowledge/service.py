"""Knowledge ingestion, retrieval, quota, and raw-view operations (Architecture §3.2).

Ingestion pipeline: source row (status pending) → chunk → embed → store chunks →
status ready. Retrieval: filter by (admin_id, instance_id, status=ready) then HNSW
ANN over the embedding, returning top-k chunks for the runtime. Quota: 5 GB total
/ 50 MB per file enforced at the API boundary (Vision §3.3).

All operations run inside a tenant_session so the three-layer isolation applies;
RLS guarantees a tenant only ever ingests/retrieves its own knowledge.
"""

from __future__ import annotations

import logging
import math
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, and_, delete, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.knowledge import pgvector
from app.knowledge.chunking import chunk_text
from app.knowledge.embeddings import get_embedder
from app.knowledge.graph_extract import GraphExtractor, extract_async, get_extractor
from app.models.audit import ActorType
from app.models.knowledge import (
    KnowledgeChunk,
    KnowledgeEdge,
    KnowledgeEntity,
    KnowledgeSource,
)

TOTAL_QUOTA_BYTES = 5 * 1000 * 1000 * 1000  # 5 GB (Vision §7)
PER_FILE_MAX_BYTES = 50 * 1000 * 1000  # 50 MB

#: Owner decision D10 (2026-09-14): a source whose sync is paused by a revoked or
#: expired credential keeps answering for this long, then stops until the owner
#: reconnects. Before round 7 WP-4c a revoked source kept answering forever.
PAUSED_SERVING_DAYS = 7

logger = logging.getLogger(__name__)


def paused_serving_cutoff(now: datetime | None = None) -> datetime:
    """A source paused at or before this instant has stopped serving (D10)."""
    return (now or datetime.now(UTC)) - timedelta(days=PAUSED_SERVING_DAYS)


def stops_serving_at(paused_since: datetime) -> datetime:
    """When a source paused at ``paused_since`` stops answering (D10)."""
    if paused_since.tzinfo is None:
        paused_since = paused_since.replace(tzinfo=UTC)
    return paused_since + timedelta(days=PAUSED_SERVING_DAYS)


class QuotaError(Exception):
    """Raised when an ingest would exceed the storage quota (→ 422 at the edge)."""


def embedding_model_name(embedder: object) -> str:
    """The model an embedder stamps on chunks (F121). Production embedders declare
    ``model_name``; a test double that does not is recorded as ``unknown`` rather
    than crashing ingestion."""
    return str(getattr(embedder, "model_name", None) or "unknown")


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
                    embedding_model=embedding_model_name(embedder),
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
    result = await extract_async(extractor, text, admin_id=admin_id)
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


def _listed(include_removed_at_origin: bool) -> ColumnElement[bool]:
    visible: ColumnElement[bool] = KnowledgeSource.soft_deleted_at.is_(None)
    if include_removed_at_origin:
        visible = or_(visible, KnowledgeSource.deleted_by == DELETED_BY_ORIGIN)
    return visible


async def list_sources(
    session: AsyncSession,
    admin_id: uuid.UUID,
    *,
    include_removed_at_origin: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> list[KnowledgeSource]:
    """Active sources, newest first; then, when asked, the ones a sync found gone at their
    origin, most recently removed first. One query, so a page can span the two."""
    statement = (
        select(KnowledgeSource)
        .where(KnowledgeSource.admin_id == admin_id, _listed(include_removed_at_origin))
        .order_by(
            KnowledgeSource.soft_deleted_at.is_not(None),
            KnowledgeSource.soft_deleted_at.desc(),
            KnowledgeSource.created_at.desc(),
            KnowledgeSource.id.desc(),
        )
    )
    if limit is not None:
        statement = statement.offset(offset).limit(limit)
    return list((await session.execute(statement)).scalars().all())


async def count_listed_sources(session: AsyncSession, admin_id: uuid.UUID) -> int:
    """How many rows the owner's list has in all, active and removed at origin."""
    return (
        await session.execute(
            select(func.count())
            .select_from(KnowledgeSource)
            .where(KnowledgeSource.admin_id == admin_id, _listed(True))
        )
    ).scalar_one()


async def sources_by_id(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    source_ids: set[uuid.UUID],
) -> dict[uuid.UUID, str]:
    """source_id -> display name, for the given ids (harmony wave 2, item 2a).

    Used by the retrieval attribution seam to resolve a real, human-readable source
    name for persisted source_chunks instead of leaving the dashboard to show a bare
    UUID. Soft-deleted sources are still resolved by name (a chunk retrieved before
    deletion should still show what it came from), just never re-retrieved.
    """
    if not source_ids:
        return {}
    rows = (
        await session.execute(
            select(KnowledgeSource.id, KnowledgeSource.name).where(
                KnowledgeSource.admin_id == admin_id,
                KnowledgeSource.instance_id == instance_id,
                KnowledgeSource.id.in_(source_ids),
            )
        )
    ).all()
    return {row[0]: row[1] for row in rows}


#: The raw view's chunk preview page (round 7 WP-4c #6). The route returns this many
#: chunks plus the REAL total, so the UI can say "first 20 of 300" instead of
#: reading the page length as the count.
CHUNK_PREVIEW_PAGE = 20


async def count_chunks(session: AsyncSession, admin_id: uuid.UUID, source_id: uuid.UUID) -> int:
    """How many chunks a source has — the honest denominator of the preview."""
    return int(
        (
            await session.execute(
                select(func.count())
                .select_from(KnowledgeChunk)
                .where(
                    KnowledgeChunk.admin_id == admin_id,
                    KnowledgeChunk.source_id == source_id,
                )
            )
        ).scalar_one()
    )


async def get_chunks(
    session: AsyncSession,
    admin_id: uuid.UUID,
    source_id: uuid.UUID,
    limit: int = CHUNK_PREVIEW_PAGE,
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
    limit: int = CHUNK_PREVIEW_PAGE,
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


DELETE_GRACE_DAYS = 30

#: Who tombstoned a source (``knowledge_sources.deleted_by``, migration 0056).
DELETED_BY_OWNER = "owner"
DELETED_BY_LIFECYCLE = "lifecycle"
#: The item disappeared at its sync origin (Drive / Notion / the crawled site) — round 7
#: WP-4c #9. That is not the owner's mis-click, so there is nothing to undo here: the
#: honest way back is re-adding it at the origin and syncing. Purged at the next
#: retention pass, not after the 30-day window (the window exists for the owner's
#: change of mind, not for content the origin no longer has). Before #9 these were
#: stamped ``owner`` — restorable for 30 days, reviving a page its origin had deleted.
DELETED_BY_ORIGIN = "origin"


class RemovedAtOrigin(Exception):
    """``restore_source`` was asked to revive an origin tombstone (round 7 WP-4c #9)."""

    def __init__(self, source: KnowledgeSource) -> None:
        super().__init__(f"{source.name} was removed at its sync origin")
        self.source = source


async def delete_source(
    session: AsyncSession,
    admin_id: uuid.UUID,
    source_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
    *,
    deleted_by: str = DELETED_BY_OWNER,
) -> bool:
    """Tombstone the source for the documented 30-day undo window (§3.2.2).

    The runtime stops retrieving from it IMMEDIATELY — every retrieval path (vector,
    graph, listing, quota) filters on ``soft_deleted_at`` — but its chunks, embeddings
    and graph rows stay for ``DELETE_GRACE_DAYS`` so ``restore_source`` has something
    to bring back. Before the 2026-09-05 audit (F109) the tombstone was written AND
    the chunks hard-deleted in the same call, so the advertised grace window had
    nothing to restore and no sweep ever purged the tombstones. The hard delete now
    happens in ``purge_expired_deletions`` (the nightly retention pass).

    ``deleted_by`` is ``owner`` (the default: the owner's own delete, undo-able for
    the window) or ``origin`` (the sync found the item gone at its source — round 7
    WP-4c #9 — not restorable, purged at the next pass). The audit payload carries
    it, with ``grace_days`` 0 for an origin tombstone so the log never advertises a
    window that does not exist.
    """
    source = (
        await session.execute(
            select(KnowledgeSource).where(
                KnowledgeSource.admin_id == admin_id, KnowledgeSource.id == source_id
            )
        )
    ).scalar_one_or_none()
    if source is None or source.soft_deleted_at is not None:
        return False
    source.soft_deleted_at = datetime.now(UTC)
    source.deleted_by = deleted_by
    await session.flush()
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=source.instance_id,
        actor_user_id=actor_user_id,
        actor_type=ActorType.admin_user if actor_user_id else ActorType.platform,
        event_type="knowledge_source_deleted",
        payload={
            "source_id": str(source_id),
            "deleted_by": deleted_by,
            "grace_days": DELETE_GRACE_DAYS if deleted_by == DELETED_BY_OWNER else 0,
        },
    )
    return True


async def restore_source(
    session: AsyncSession,
    admin_id: uuid.UUID,
    source_id: uuid.UUID,
    actor_user_id: uuid.UUID | None = None,
) -> KnowledgeSource | None:
    """Undo an owner delete inside its grace window (§3.2.2).

    Only OWNER tombstones are restorable here: a source tombstoned by the Luciel
    delete (``deleted_by = lifecycle``) comes back with the Luciel, through
    ``state_machine.restore`` — and never through this call, which would otherwise
    revive one source of a Luciel that is itself deleted. Returns None when there is
    nothing to restore (unknown id, not deleted, lifecycle-deleted, or already purged).
    An ORIGIN tombstone (the item is gone at Drive / Notion / the site — round 7 WP-4c
    #9) raises ``RemovedAtOrigin`` so the caller can say so honestly instead of
    "not in its undo window": there never was a window, and reviving it would serve
    a page its origin deleted.
    """
    source = (
        await session.execute(
            select(KnowledgeSource).where(
                KnowledgeSource.admin_id == admin_id, KnowledgeSource.id == source_id
            )
        )
    ).scalar_one_or_none()
    if source is None or source.soft_deleted_at is None:
        return None
    if source.deleted_by == DELETED_BY_ORIGIN:
        raise RemovedAtOrigin(source)
    if source.deleted_by != DELETED_BY_OWNER:
        return None
    source.soft_deleted_at = None
    source.deleted_by = None
    await session.flush()
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=source.instance_id,
        actor_user_id=actor_user_id,
        actor_type=ActorType.admin_user if actor_user_id else ActorType.platform,
        event_type="knowledge_source_restored",
        payload={"source_id": str(source_id)},
    )
    return source


async def keep_original(
    session: AsyncSession,
    *,
    source: KnowledgeSource,
    data: bytes,
    filename: str,
    content_type: str,
) -> None:
    """Keep the uploaded bytes so the account export can return them (§5.10, F016).

    Best-effort against the store: a storage blip must not fail an ingest that already
    succeeded — the row simply carries no original, the export says so for that file,
    and the failure is on the log the alarms read.
    """
    from app.knowledge.original_store import (  # noqa: PLC0415 — avoid import cycle
        get_original_store,
        original_key_for,
    )

    key = original_key_for(source.admin_id, source.id, filename)
    try:
        await get_original_store().put(key=key, data=data, content_type=content_type)
    except Exception:  # noqa: BLE001 — see docstring
        logger.exception(
            "original upload keep failed admin_id=%s source=%s", source.admin_id, source.id
        )
        return
    source.original_key = key
    source.original_content_type = content_type
    await session.flush()


async def _hard_delete_source_rows(
    session: AsyncSession, *, admin_id: uuid.UUID, source_id: uuid.UUID
) -> None:
    """Remove a source's chunks, graph rows, stored original and the source row itself
    (no audit here — the caller records the reason)."""
    from app.knowledge.original_store import get_original_store  # noqa: PLC0415

    source = (
        await session.execute(
            select(KnowledgeSource.original_key).where(
                KnowledgeSource.admin_id == admin_id, KnowledgeSource.id == source_id
            )
        )
    ).scalar_one_or_none()
    if source:
        try:
            await get_original_store().delete(key=source)
        except Exception:  # noqa: BLE001 — the tenant prefix sweep at the cascade is the backstop
            logger.exception("original delete failed admin_id=%s source=%s", admin_id, source_id)
    await session.execute(
        delete(KnowledgeChunk).where(
            KnowledgeChunk.admin_id == admin_id, KnowledgeChunk.source_id == source_id
        )
    )
    # Edges first (they reference entities), then the entities (§3.2.1 rule 5).
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
    await session.execute(
        delete(KnowledgeSource).where(
            KnowledgeSource.admin_id == admin_id, KnowledgeSource.id == source_id
        )
    )


async def purge_expired_deletions(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    now: datetime | None = None,
    grace_days: int = DELETE_GRACE_DAYS,
) -> int:
    """Hard-delete owner-tombstoned sources whose undo window has closed, and every
    origin tombstone (round 7 WP-4c #9) regardless of age — an item gone at its
    sync origin never had a window.

    Returns the number of sources destroyed. Lifecycle tombstones are not touched —
    they belong to the Luciel's own grace window and its day-30 cascade. Idempotent.
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=grace_days)
    expired = (
        (
            await session.execute(
                select(KnowledgeSource).where(
                    KnowledgeSource.admin_id == admin_id,
                    KnowledgeSource.soft_deleted_at.is_not(None),
                    or_(
                        and_(
                            KnowledgeSource.deleted_by == DELETED_BY_OWNER,
                            KnowledgeSource.soft_deleted_at < cutoff,
                        ),
                        KnowledgeSource.deleted_by == DELETED_BY_ORIGIN,
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    for source in expired:
        await _hard_delete_source_rows(session, admin_id=admin_id, source_id=source.id)
    if expired:
        await session.flush()
    return len(expired)


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
    action: str = "replace",
) -> KnowledgeSource | None:
    """Replace affordance (Architecture §3.2.2): re-ingest a NEW file into an
    EXISTING source row. The source id/name/place-in-list is preserved but its
    chunks, embeddings, and graph nodes/edges are fully replaced by the new
    content — this is the "corrected source is re-ingested, the chunks are
    updated" root-cause-fix flow (§5.2/§9). Returns None if the source does not
    exist for this tenant. Quota is re-checked against the NEW size, crediting
    back the old size first so a same-size or smaller replace never false-fails.

    ``action`` labels the audit row: ``replace`` for the owner's affordance,
    ``sync_update`` when a live-sync revision change re-ingests through this same
    path (round 7 WP-4c #2 — the sync used to re-embed inline, skipping the quota
    check, the size bookkeeping and the graph re-extraction this function does).
    A ``QuotaError`` is raised BEFORE anything is wiped, so a refused replace leaves
    the previous chunks and graph serving.
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
                    embedding_model=embedding_model_name(embedder),
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
        payload={"source_id": str(source_id), "action": action, "chunks": len(chunks)},
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
    A source paused by a revoked credential for more than PAUSED_SERVING_DAYS is
    excluded too (D10): its content is no longer being kept true.

    Implements the §3.2 retrieval flow in full:
      step 2 — HNSW ANN over the embedding (ordered by cosine distance);
      step 3 — re-rank the top candidates by recency + chunk-ordinal proximity as a
               tie-break within a narrow cosine band, then truncate to k.
    """
    embedder = get_embedder()
    qvec = (await embedder.embed([query]))[0]
    distance = KnowledgeChunk.embedding.cosine_distance(qvec)
    # One global HNSW index, tenant filter after the walk (round 6 WP-G, F044): widen the
    # walk with k and let pgvector 0.8+ keep walking until the filtered set is full, so a
    # small tenant beside a large one still gets k rows. Transaction-local settings.
    for stmt in pgvector.search_settings_sql(
        k + _RERANK_OVERFETCH, await pgvector.installed_version(session)
    ):
        await session.execute(text(stmt))
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
                or_(
                    KnowledgeSource.paused_since.is_(None),
                    KnowledgeSource.paused_since > paused_serving_cutoff(),
                ),
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


#: A same-model embedding of the same text lands within a hair of the stored vector;
#: anything past this is a vector that was never this text under this model.
EMBEDDING_VERIFY_MAX_DISTANCE = 0.2


def cosine_distance(a: list[float], b: list[float]) -> float:
    """1 - cosine similarity; 1.0 when either vector is empty or all zeros."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 1.0
    return 1.0 - dot / (na * nb)


def _rewriting_live_vectors_with_mock(current: str, label: str | None) -> bool:
    """True when a MOCK embedder is about to overwrite a vector the live model produced
    (2026-09-13: a sweep run from a mock-mode container re-embedded a live tenant's
    chunks with hash vectors). The live model name is the configured pin."""
    from app.config import get_settings  # noqa: PLC0415 — import cycle

    live_model = str(get_settings().knowledge_embedding_model or "text-embedding-3-small")
    return current == "hashing-mock" and label == live_model


async def verify_chunk_embeddings(
    session: AsyncSession, *, admin_id: uuid.UUID, limit: int = 20
) -> tuple[int, int]:
    """Check a bounded batch of never-verified chunks against a fresh embedding of their
    own text and re-embed the ones that disagree (round 6 F121b).

    The model label alone cannot catch a vector that came from another embedder but
    carries the current name (migration 0058 backfilled every unlabelled row with the
    live model), or a vector that is missing. Such a chunk sits past the relevance floor
    on every query — the owner's knowledge is silently dead. Returns (checked, repaired);
    every checked row is stamped so the pass converges over sweeps."""
    embedder = get_embedder()
    current = embedding_model_name(embedder)
    rows = (
        (
            await session.execute(
                select(KnowledgeChunk)
                .where(
                    KnowledgeChunk.admin_id == admin_id,
                    KnowledgeChunk.embedding_verified_at.is_(None),
                )
                .order_by(KnowledgeChunk.created_at, KnowledgeChunk.ordinal)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    rows = [r for r in rows if not _rewriting_live_vectors_with_mock(current, r.embedding_model)]
    if not rows:
        return 0, 0
    fresh = await embedder.embed([r.text for r in rows])
    now = datetime.now(UTC)
    repaired = 0
    for row, vec in zip(rows, fresh, strict=True):
        stored = list(row.embedding) if row.embedding is not None else []
        if not stored or cosine_distance(stored, list(vec)) > EMBEDDING_VERIFY_MAX_DISTANCE:
            row.embedding = vec
            row.embedding_model = current
            repaired += 1
        row.embedding_verified_at = now
    await session.flush()
    return len(rows), repaired


async def reembed_outdated_chunks(
    session: AsyncSession, *, admin_id: uuid.UUID, limit: int = 200
) -> int:
    """Re-embed chunks whose vectors came from a model other than the current pin
    (2026-09-05 audit F121). Bounded per pass so a model change re-embeds a tenant's
    knowledge over a few sweeps instead of one unbounded transaction. Returns the
    number of chunks rewritten."""
    embedder = get_embedder()
    current = embedding_model_name(embedder)
    rows = (
        (
            await session.execute(
                select(KnowledgeChunk)
                .where(
                    KnowledgeChunk.admin_id == admin_id,
                    KnowledgeChunk.embedding_model.is_distinct_from(current),
                )
                .order_by(KnowledgeChunk.source_id, KnowledgeChunk.ordinal)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    rows = [r for r in rows if not _rewriting_live_vectors_with_mock(current, r.embedding_model)]
    if not rows:
        return 0
    vectors = await embedder.embed([r.text for r in rows])
    now = datetime.now(UTC)
    for row, vec in zip(rows, vectors, strict=True):
        row.embedding = vec
        row.embedding_model = current
        row.embedding_verified_at = now  # a fresh same-model vector is verified by construction
    await session.flush()
    return len(rows)
