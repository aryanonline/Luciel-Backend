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

import logging
import uuid
import zlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select, text
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
from app.models.connection import InstanceConnection
from app.models.instance import NON_SERVING_STATES, Instance
from app.models.knowledge import KnowledgeSource

logger = logging.getLogger(__name__)

#: ``SyncResult.halted`` when the Luciel stopped serving while the provider fetch was
#: on the wire (paused or deleted meanwhile): the run ended before any write.
HALTED_NOT_SERVING = "instance_not_serving"

#: A connection in one of these states was taken away by the owner. A sync that finds
#: one mid-run stops and keeps nothing (``SyncStoppedByOwner``); ``error``, ``connected``
#: and the rest keep syncing, because the owner did not ask for anything to stop.
REMOVED_STATUSES = frozenset({"not_connected", "revoked"})

#: The advisory-lock namespace of "one reconcile per connection" (the first int4 key).
SYNC_LOCK_K1 = zlib.crc32(b"knowledge_sync_job") - 2**31


def sync_lock_k2(connection_id: uuid.UUID) -> int:
    """The second int4 key: the connection. Derived in Python, like the sweeps' keys
    (``hashtext`` is not stable across Postgres versions)."""
    return int.from_bytes(connection_id.bytes[:4], "big") - 2**31


async def claim_sync_lock(session: AsyncSession, connection_id: uuid.UUID) -> bool:
    """Claim this connection's reconcile for the current transaction, or report it taken.

    ``knowledge_sources`` has no unique (connection_id, external_id), so two overlapping
    reconciles of one connection each ingest the pages the other has not committed and
    leave duplicates. The lock is the database's own word on "a sync is running": it
    dies with the transaction, on commit, rollback or a killed process, so an orphan
    cannot exist. Reentrant inside one transaction. A lock FIRST taken inside a SAVEPOINT
    is released when that savepoint rolls back, so a caller that must keep it past a
    failed sync takes it before its savepoint."""
    return bool(
        (
            await session.execute(
                text("SELECT pg_try_advisory_xact_lock(:k1, :k2)"),
                {"k1": SYNC_LOCK_K1, "k2": sync_lock_k2(connection_id)},
            )
        ).scalar_one()
    )


async def live_sync_lock_keys(session: AsyncSession) -> frozenset[int]:
    """The ``sync_lock_k2`` of every reconcile running right now, in this database.

    ``pg_locks`` shows a two-key advisory lock as classid = k1, objid = k2, objsubid = 2,
    each an unsigned ``oid`` holding the bits of the signed int4, so k1 is converted to
    unsigned going in and every k2 back to signed coming out. Not tenant data: the keys
    are bare integers and the caller only ever tests its own connection's."""
    rows = await session.execute(
        text(
            "SELECT CAST(objid AS bigint) FROM pg_locks "
            "WHERE locktype = 'advisory' AND objsubid = 2 AND granted "
            "AND classid = CAST(CAST(:k1 AS bigint) AS oid) "
            "AND database = (SELECT oid FROM pg_database WHERE datname = current_database())"
        ),
        {"k1": SYNC_LOCK_K1 & 0xFFFFFFFF},
    )
    return frozenset(
        unsigned - 2**32 if unsigned >= 2**31 else unsigned for (unsigned,) in rows.all()
    )


class SyncAlreadyRunning(Exception):
    """Another reconcile of this connection holds its lock; this one ran nothing."""


class SyncStoppedByOwner(Exception):
    """The owner removed the connection while this reconcile ran. The caller rolls back
    to its savepoint, so nothing from the run is kept."""


async def connection_removed(
    session: AsyncSession, *, admin_id: uuid.UUID, connection_id: uuid.UUID
) -> bool:
    """Whether the owner has taken this connection away, read from the database.

    A column select, not an entity load: under READ COMMITTED this sees a disconnect
    another transaction committed a moment ago. A missing row is not "removed" here."""
    status = (
        await session.execute(
            select(InstanceConnection.status).where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.connection_id == connection_id,
            )
        )
    ).scalar_one_or_none()
    return status in REMOVED_STATUSES


async def _stop_if_removed(
    session: AsyncSession, *, admin_id: uuid.UUID, connection_id: uuid.UUID
) -> None:
    if await connection_removed(session, admin_id=admin_id, connection_id=connection_id):
        raise SyncStoppedByOwner(str(connection_id))


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
        # Set when the run ended WITHOUT writing anything because the Luciel stopped
        # serving while the provider fetch was on the wire (``HALTED_NOT_SERVING``).
        # Not a failure: nothing is flagged, the next pass after a resume/restore
        # reconciles as usual.
        self.halted: str | None = None
        # Items kept and flagged "error" this pass because the provider could not read
        # them (``failed_ids``, F117) or never reached them (knowledge #3). Recorded on
        # the sync job (``app.knowledge.sync_job``) for the owner; not on the 200 body.
        self.failed: list[str] = []
        self.unreached: list[str] = []

    def job_dict(self) -> dict[str, object]:
        """The full pass for the connection's ``sync_job.result`` (round 7 walk run 8):
        the wire four plus what the audit row and the sweep log already carried."""
        return {
            **self.as_dict(),
            "failed": self.failed,
            "unreached": self.unreached,
            "graph_deferred": self.graph_deferred,
            "halted": self.halted,
        }

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


async def _instance_serving(
    session: AsyncSession, *, admin_id: uuid.UUID, instance_id: uuid.UUID
) -> bool:
    """Whether the Luciel is serving RIGHT NOW, read from the database.

    A column select, not an entity load: the caller's session may already hold the
    Instance in its identity map (the sweep loads it to decide whether to start), and
    a re-query of the entity would hand back that stale object. Under READ COMMITTED
    this statement sees a pause or delete another transaction committed while the
    provider fetch was on the wire. A missing row reads as not serving.
    """
    state = (
        await session.execute(
            select(Instance.state).where(Instance.admin_id == admin_id, Instance.id == instance_id)
        )
    ).scalar_one_or_none()
    return state is not None and state not in NON_SERVING_STATES


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

    In-flight lifecycle (round 7 M10): the sweep skips a non-serving Luciel when it
    STARTS, but the provider fetch can take minutes (a crawl), and the owner may pause
    or delete the Luciel meanwhile. The serving state is re-read after the snapshot
    returns and BEFORE any write: a Luciel that was serving when the run began and is
    not serving now ends the run with no writes at all — no chunks, no tombstones, no
    status flags, no audit row — and ``result.halted`` says why. The sources are not
    marked failed (nothing failed); the first pass after a resume or restore
    reconciles as usual. A run the owner started on a Luciel that was ALREADY paused
    (the manual Re-sync while preparing knowledge) proceeds as before: nothing
    changed under it.

    One reconcile per connection (round 7 walk run 8): the advisory lock is the first
    statement, whichever of the three callers started this, and a refusal raises
    ``SyncAlreadyRunning`` having done nothing.

    Removing a source stops its sync: the connection's status is re-read after the
    snapshot, before each item's ingest and once more before returning. Not connected
    or revoked raises ``SyncStoppedByOwner``; the caller's savepoint unwinds the run, so
    no page lands after "no longer crawled".
    """
    if not await claim_sync_lock(session, connection_id):
        raise SyncAlreadyRunning(str(connection_id))
    result = SyncResult()
    serving_at_start = await _instance_serving(session, admin_id=admin_id, instance_id=instance_id)
    snapshot = await provider.snapshot(connection_id)
    await _stop_if_removed(session, admin_id=admin_id, connection_id=connection_id)
    if serving_at_start and not await _instance_serving(
        session, admin_id=admin_id, instance_id=instance_id
    ):
        result.halted = HALTED_NOT_SERVING
        logger.info(
            "knowledge_sync_halted reason=%s origin=%s items=%d",
            HALTED_NOT_SERVING,
            origin,
            len(snapshot.items),
        )
        return result
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
    result.failed = sorted(failed_ids)
    result.unreached = sorted(unreached)

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
        if prior is None or prior.original_object_key != item.revision:
            await _stop_if_removed(session, admin_id=admin_id, connection_id=connection_id)
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
    await _stop_if_removed(session, admin_id=admin_id, connection_id=connection_id)
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
