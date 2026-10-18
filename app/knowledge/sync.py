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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.knowledge import service
from app.knowledge.graph_extract import (
    ExtractionResult,
    GraphExtractor,
    extract_async,
    get_extractor,
)
from app.models.audit import ActorType
from app.models.knowledge import KnowledgeSource


@dataclass
class RemoteItem:
    external_id: str
    name: str
    text: str
    # Provider change marker (Drive page-token delta / Notion last_edited_time / etc.).
    revision: str


@dataclass
class RemoteSnapshot:
    """What a provider reports for the connected scope at a point in time.

    ``failed_ids`` — items the provider KNOWS exist but could not read this pass (a
    page that timed out, a file whose download failed). The sync keeps their sources
    and flags them, instead of reading "absent" and retiring them (2026-09-05 audit
    F117: a partial crawl used to delete every page it did not reach).
    """

    items: list[RemoteItem] = field(default_factory=list)
    failed_ids: list[str] = field(default_factory=list)
    # Items the provider KNOWS exist but did not visit because its budget ended the
    # walk before them (a crawl's depth cap — round 7 Phase 2 review, knowledge #3).
    # Kept and flagged exactly like ``failed_ids`` (F117).
    unreached_ids: list[str] = field(default_factory=list)
    # False when the provider stopped enumerating before its scope was exhausted and
    # cannot say what lies beyond (a crawl's page cap tripped, or a page whose links
    # were never read): an item absent from an incomplete snapshot was not REACHED,
    # which is not evidence it was removed at the origin. Every absent item is then
    # kept and flagged, never tombstoned as removed at origin and purged.
    complete: bool = True


class SnapshotShrunk(Exception):
    """The provider reported far fewer items than are synced (2026-09-05 audit F107).

    An empty listing from a revoked scope, a provider outage answering 200 with
    nothing, or an owner who moved a folder used to be read as "everything was
    deleted at the source" — and the sync retired every source. Nothing is deleted
    on a shrink; the sources are flagged and the owner confirms an intentional one
    (``allow_shrink``)."""

    def __init__(self, *, existing: int, remote: int) -> None:
        super().__init__(f"source reports {remote} items where {existing} are synced")
        self.existing = existing
        self.remote = remote


#: Below this fraction of the synced count, a snapshot is a shrink, not a diff.
SHRINK_FLOOR = 0.5


class SyncProvider(Protocol):
    """Fetches the current in-scope items for a connection. Real impls use OAuth
    tokens from Secrets Manager via the Connections layer (Arc 17)."""

    origin: str  # 'google_drive' | 'notion' | 'crm_kb' | 'website_crawl'

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot: ...


class _GraphPass:
    """The bound graph extractor for ONE synced item, remembering whether its LLM pass
    was skipped for budget (``ExtractionResult.llm_skipped``) so the sync can leave
    that item's revision marker unbumped — a skipped page is re-extracted next pass
    instead of keeping a grammar-only graph until its content happens to change."""

    def __init__(self, inner: GraphExtractor) -> None:
        self._inner = inner
        self.llm_skipped = False

    def extract(self, text: str) -> ExtractionResult:
        return self._inner.extract(text)

    async def aextract(self, text: str, *, admin_id: uuid.UUID | None = None) -> ExtractionResult:
        result = await extract_async(self._inner, text, admin_id=admin_id)
        self.llm_skipped = result.llm_skipped
        return result


class SyncResult:
    def __init__(self) -> None:
        self.added: list[str] = []
        self.updated: list[str] = []
        self.removed: list[str] = []
        # Items the quota refused this pass (round 7 WP-4c #2): a new item over the
        # per-file cap or the tenant total is not ingested; a changed item that grew
        # past either keeps its PREVIOUS chunks serving and reads ``sync_status =
        # "error"`` — never a silently truncated or silently absent page.
        self.refused: list[str] = []
        # Items ingested this pass whose LLM graph pass was skipped for the tenant's
        # per-minute budget (graph_extract.INGEST_HEADROOM_FRACTION): their chunks are
        # served now, their graph is grammar-only, and their revision marker is left
        # unbumped so the next pass re-extracts them.
        self.graph_deferred: list[str] = []

    def as_dict(self) -> dict[str, list[str]]:
        # ``refused`` travels with the other three (round 7 Phase 2 review, knowledge
        # #4): the manual re-sync answered 200 with a quota-refused page silently
        # absent, because only the audit payload carried it.
        return {
            "added": self.added,
            "updated": self.updated,
            "removed": self.removed,
            "refused": self.refused,
        }


async def sync_connection(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    connection_id: uuid.UUID,
    origin: str,
    provider: SyncProvider,
    allow_shrink: bool = False,
) -> SyncResult:
    """Reconcile knowledge_sources for one synced connection against the provider.

    Incremental by external_id + revision: new items are ingested, changed items
    re-ingested (chunks replaced), and items no longer present at the source are
    removed — so Luciel stops answering from deleted/out-of-scope content (§3.2.3).

    A snapshot that would retire more than half of what is synced is refused
    (``SnapshotShrunk``) unless ``allow_shrink`` says the owner confirmed it (F107);
    items the provider could not read this pass (``failed_ids``) are kept and
    flagged, never retired (F117).
    """
    result = SyncResult()
    snapshot = await provider.snapshot(connection_id)
    remote_by_id = {item.external_id: item for item in snapshot.items}
    failed_ids = set(snapshot.failed_ids)

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
    # Absent items the provider cannot vouch for: unreadable this pass (F117), known
    # but beyond its budget, or — under an incomplete snapshot — any absent item at
    # all (knowledge #3). Kept and flagged, never retired.
    absent = {e for e in existing_by_ext if e not in remote_by_id and e not in failed_ids}
    unreached = absent if not snapshot.complete else absent & set(snapshot.unreached_ids)
    unvouched = failed_ids | unreached

    # Floor guard (F107): a listing that lost more than half of what is synced — or
    # everything — is treated as a broken read, not a mass deletion. Nothing is
    # touched; the sources read "error" until the owner re-syncs with confirmation.
    survivors = len([e for e in existing_by_ext if e in remote_by_id or e in unvouched])
    if existing_by_ext and not allow_shrink and survivors < SHRINK_FLOOR * len(existing_by_ext):
        # The refusal is durable evidence for the owner (the rows read 'error', the
        # audit row says why), so it is written in its OWN transaction: the caller's
        # session unwinds with the exception and must not take the evidence with it.
        from app.db.session import tenant_session  # noqa: PLC0415 — avoids a db↔knowledge cycle

        async with tenant_session(admin_id) as evidence:
            await mark_sync_error(evidence, admin_id=admin_id, connection_id=connection_id)
            await audit.emit(
                evidence,
                admin_id=admin_id,
                instance_id=instance_id,
                actor_type=ActorType.platform,
                event_type="knowledge_source_edited",
                payload={
                    "action": "sync_refused_shrink",
                    "connection_id": str(connection_id),
                    "origin": origin,
                    "synced": len(existing_by_ext),
                    "reported": len(remote_by_id),
                    "unreadable": len(failed_ids),
                },
            )
        raise SnapshotShrunk(existing=len(existing_by_ext), remote=len(remote_by_id))

    # The provider answered, so the credential is live: whatever revocation paused
    # these sources is over (D10). Cleared here, inside the caller's transaction, so
    # a reconcile that fails further down unwinds the clearing with everything else
    # and nothing reads "serving again" that was never re-verified.
    for src in existing:
        src.paused_since = None

    # Additions + updates. Both go through the SAME ingest functions the owner's
    # upload/replace affordances use (round 7 WP-4c #2): the per-file cap, the tenant
    # total, ``size_bytes`` and the graph extraction all apply to synced content
    # exactly as they do to uploaded content. The update path used to re-embed inline
    # — no quota, a size_bytes frozen at the first sync, a graph that never followed
    # the page — so a synced page could grow past every limit unnoticed.
    for ext_id, item in remote_by_id.items():
        prior = existing_by_ext.get(ext_id)
        graph = _GraphPass(get_extractor())
        if prior is None:
            try:
                src = await service.ingest_text(
                    session,
                    admin_id=admin_id,
                    instance_id=instance_id,
                    name=item.name,
                    text=item.text,
                    origin=origin,
                    extractor=graph,
                )
            except service.QuotaError:
                # Nothing was written for this item (ingest_text checks before it
                # inserts). It is not synced; the audit row below says so.
                result.refused.append(ext_id)
                continue
            src.connection_id = connection_id
            src.external_id = ext_id
            src.last_synced_at = datetime.now(UTC)
            src.sync_status = "synced"
            # store revision in name-adjacent field via non-destructive approach.
            # A graph pass skipped for budget leaves NO revision, so the next pass
            # reads the item as changed and re-extracts it.
            src.original_object_key = None if graph.llm_skipped else item.revision
            if graph.llm_skipped:
                result.graph_deferred.append(ext_id)
            result.added.append(ext_id)
        elif prior.original_object_key != item.revision:
            # Changed: re-ingest through replace_source — quota (crediting the old
            # size), chunks, size_bytes, graph — under the same source id.
            try:
                await service.replace_source(
                    session,
                    admin_id,
                    prior.id,
                    text=item.text,
                    size_bytes=len(item.text.encode("utf-8")),
                    action="sync_update",
                    extractor=graph,
                )
            except service.QuotaError:
                # The previous revision keeps serving (replace_source refuses before
                # it wipes); the row reads the honest error state until the page
                # shrinks or the quota frees up. The revision marker is NOT bumped,
                # so the next pass tries again.
                prior.sync_status = "error"
                prior.last_synced_at = datetime.now(UTC)
                result.refused.append(ext_id)
                continue
            prior.name = item.name
            if graph.llm_skipped:
                # The new content serves now; the marker stays on the OLD revision
                # so the next pass re-extracts the graph the budget deferred.
                result.graph_deferred.append(ext_id)
            else:
                prior.original_object_key = item.revision
            prior.last_synced_at = datetime.now(UTC)
            prior.sync_status = "synced"
            result.updated.append(ext_id)
        else:
            prior.last_synced_at = datetime.now(UTC)
            # A source that read "error" / "paused" and now syncs cleanly is synced
            # again (2026-09-05 audit F108): the flag used to outlive the failure.
            prior.sync_status = "synced"

    # Deletions: items present locally but gone from the source (§3.2.3 safety).
    # Unreadable-this-pass items (F117) and items an incomplete snapshot never reached
    # (knowledge #3) are kept and flagged, never retired.
    for ext_id, src in existing_by_ext.items():
        if ext_id in unvouched:
            src.sync_status = "error"
            continue
        if ext_id not in remote_by_id:
            # Tagged ``origin``, not ``owner`` (round 7 WP-4c #9): the owner did not
            # choose this, so restore refuses it honestly and the retention pass
            # purges it at the next run instead of holding a 30-day undo window
            # for a page its origin no longer has.
            await service.delete_source(
                session, admin_id, src.id, deleted_by=service.DELETED_BY_ORIGIN
            )
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
            "graph_deferred": result.graph_deferred,
            "unreached": sorted(unreached),
        },
    )
    return result


async def pause_sync_on_revocation(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    connection_id: uuid.UUID,
    now: datetime | None = None,
) -> int:
    """On revoked/expired connection, flag synced sources paused_reconnect_needed and pause.

    Never silently keep serving the last-synced copy (§3.2.3): the sources are
    marked paused_reconnect_needed so the raw view shows the honest state, and the
    D10 clock starts (``paused_since``): they keep answering for
    ``service.PAUSED_SERVING_DAYS`` and then stop until a clean sync. The clock is
    set ONCE per incident — the health sweep and a manual re-sync both pause, and
    the second pause must not push the stop date out.
    """
    now = now or datetime.now(UTC)
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
        if s.paused_since is None:
            s.paused_since = now
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
