"""Live-sync knowledge connectors (Architecture §3.2.3, Arc 11b).

A knowledge-sync connector is an instance_connections entry of connection_type
'knowledge_source' (§3.8). This module owns the sync MECHANICS that sit on top of
that connection:
  - initial sync: enumerate in-scope items → ingest each (parse→chunk→embed).
  - incremental sync: pull only what changed via the provider's change primitive
    (Drive changes feed, Notion last_edited_time, CRM updatedAt; website crawl =
    full re-fetch + diff).
  - deletion / permission-revocation propagation: an item removed at source (or a
    revoked connection) removes/pauses the corresponding knowledge_source rather
    than silently serving stale content.
  - freshness: every synced source carries last_synced_at + sync_status.

Provider FETCH (the actual Drive/Notion/CRM API calls + OAuth token use) depends
on the Connections layer (Arc 17) for credentials. Here the fetch is abstracted
behind ``SyncProvider``; a deterministic in-memory provider exercises the full
incremental/deletion machinery without external APIs. Real providers register the
same interface in Arc 17/11b integration.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.knowledge import service
from app.models.audit import ActorType
from app.models.knowledge import KnowledgeChunk, KnowledgeSource


@dataclass
class RemoteItem:
    external_id: str
    name: str
    text: str
    # Provider change marker (Drive page-token delta / Notion last_edited_time / etc.).
    revision: str


@dataclass
class RemoteSnapshot:
    """What a provider reports for the connected scope at a point in time."""

    items: list[RemoteItem] = field(default_factory=list)


class SyncProvider(Protocol):
    """Fetches the current in-scope items for a connection. Real impls use OAuth
    tokens from Secrets Manager via the Connections layer (Arc 17)."""

    origin: str  # 'google_drive' | 'notion' | 'crm_kb' | 'website_crawl'

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot: ...


class SyncResult:
    def __init__(self) -> None:
        self.added: list[str] = []
        self.updated: list[str] = []
        self.removed: list[str] = []

    def as_dict(self) -> dict[str, list[str]]:
        return {"added": self.added, "updated": self.updated, "removed": self.removed}


async def sync_connection(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    connection_id: uuid.UUID,
    origin: str,
    provider: SyncProvider,
) -> SyncResult:
    """Reconcile knowledge_sources for one synced connection against the provider.

    Incremental by external_id + revision: new items are ingested, changed items
    re-ingested (chunks replaced), and items no longer present at the source are
    removed — so Luciel stops answering from deleted/out-of-scope content (§3.2.3).
    """
    result = SyncResult()
    snapshot = await provider.snapshot(connection_id)
    remote_by_id = {item.external_id: item for item in snapshot.items}

    # Existing synced sources for this connection.
    existing = (
        (
            await session.execute(
                select(KnowledgeSource).where(
                    KnowledgeSource.admin_id == admin_id,
                    KnowledgeSource.connection_id == connection_id,
                    KnowledgeSource.soft_deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    existing_by_ext = {s.external_id: s for s in existing if s.external_id}

    # Additions + updates.
    for ext_id, item in remote_by_id.items():
        prior = existing_by_ext.get(ext_id)
        if prior is None:
            src = await service.ingest_text(
                session,
                admin_id=admin_id,
                instance_id=instance_id,
                name=item.name,
                text=item.text,
                origin=origin,
            )
            src.connection_id = connection_id
            src.external_id = ext_id
            src.last_synced_at = datetime.now(UTC)
            src.sync_status = "synced"
            # store revision in name-adjacent field via non-destructive approach:
            src.original_object_key = item.revision
            result.added.append(ext_id)
        elif prior.original_object_key != item.revision:
            # Changed: replace chunks (re-chunk + re-embed) and bump revision.
            await session.execute(
                delete(KnowledgeChunk).where(
                    KnowledgeChunk.admin_id == admin_id,
                    KnowledgeChunk.source_id == prior.id,
                )
            )
            from app.knowledge.chunking import chunk_text
            from app.knowledge.embeddings import get_embedder

            chunks = chunk_text(item.text)
            vectors = await get_embedder().embed(chunks) if chunks else []
            for ordinal, (c, vec) in enumerate(zip(chunks, vectors, strict=True)):
                session.add(
                    KnowledgeChunk(
                        id=uuid.uuid4(),
                        admin_id=admin_id,
                        instance_id=instance_id,
                        source_id=prior.id,
                        ordinal=ordinal,
                        text=c,
                        embedding=vec,
                    )
                )
            prior.name = item.name
            prior.original_object_key = item.revision
            prior.last_synced_at = datetime.now(UTC)
            prior.sync_status = "synced"
            prior.last_updated_at = datetime.now(UTC)
            result.updated.append(ext_id)
        else:
            prior.last_synced_at = datetime.now(UTC)

    # Deletions: items present locally but gone from the source (§3.2.3 safety).
    for ext_id, src in existing_by_ext.items():
        if ext_id not in remote_by_id:
            await service.delete_source(session, admin_id, src.id)
            result.removed.append(ext_id)

    await session.flush()
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        actor_type=ActorType.platform,
        event_type="knowledge_source_edited",
        payload={
            "action": "sync",
            "connection_id": str(connection_id),
            "origin": origin,
            **result.as_dict(),
        },
    )
    return result


async def pause_sync_on_revocation(
    session: AsyncSession, *, admin_id: uuid.UUID, connection_id: uuid.UUID
) -> int:
    """On revoked/expired connection, flag synced sources paused_reconnect_needed and pause.

    Never silently keep serving the last-synced copy (§3.2.3): the sources are
    marked paused_reconnect_needed so the raw view shows the honest state.
    """
    sources = (
        (
            await session.execute(
                select(KnowledgeSource).where(
                    KnowledgeSource.admin_id == admin_id,
                    KnowledgeSource.connection_id == connection_id,
                    KnowledgeSource.soft_deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for s in sources:
        s.sync_status = "paused_reconnect_needed"  # §3.2.3 vocabulary (Axis4-F02)
    await session.flush()
    return len(sources)


async def mark_sync_error(
    session: AsyncSession, *, admin_id: uuid.UUID, connection_id: uuid.UUID
) -> int:
    """Flag this connection's live sources ``sync_status = "error"`` (round 5B
    item 12). The wire vocabulary carried "error" since §3.2.3 but nothing ever
    WROTE it — a repeatedly failing non-credential sync (quota, parse, provider
    500) read "synced" with a quietly aging last_synced_at forever. The raw
    knowledge view now shows the honest state; the failure detail stays in the
    sweep log (it may carry provider wording — never surfaced raw)."""
    sources = (
        (
            await session.execute(
                select(KnowledgeSource).where(
                    KnowledgeSource.admin_id == admin_id,
                    KnowledgeSource.connection_id == connection_id,
                    KnowledgeSource.soft_deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    for s in sources:
        s.sync_status = "error"
    await session.flush()
    return len(sources)
