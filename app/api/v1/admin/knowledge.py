"""/api/v1/admin/knowledge/* — raw knowledge view + ingestion (Architecture §3.2).

Frozen contract (http-admin.ts) exposes exactly: listSources, getChunks, quota,
deleteSource, resyncSource. Those are implemented field-for-field.

INGEST endpoints (upload / paste) are ADDITIVE — the frozen admin client does not
yet model a knowledge.create method, so adding these new endpoints does not break
the swap-with-zero-component-changes property (Space Instructions §7; additive
changes within v1 are allowed, Architecture §5.9.2). Flagged in BUILD_SPEC for a
later coordinated frontend addition. The Customer Journey §4.3 upload/paste/CSV
flows are real product behavior these back.
"""

from __future__ import annotations

import uuid
from typing import cast

import httpx
from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Subject, current_admin
from app.api.errors import conflict, not_found, validation_error
from app.api.paging import PAGE_MAX, SOURCES_MAX, TOTAL_COUNT_HEADER
from app.audit import log as audit
from app.connections import oauth_flow, registry
from app.connections import service as connections_service
from app.connections.secrets import get_secret_store
from app.connections.verify import POST_EXCHANGE_VERIFIERS, PostExchangeVerifyFailed
from app.db.session import tenant_session
from app.knowledge import scope as knowledge_scope
from app.knowledge import service, sync
from app.knowledge.parsers import ParseError, parse_file
from app.knowledge.providers import (
    CrawlUnreachable,
    HttpTransport,
    OAuthExchangeError,
    ProviderUnavailable,
    exchange_code_for_token,
    list_scope_candidates,
    provider_for_connection,
    store_oauth_token,
)
from app.knowledge.service import QuotaError
from app.models.audit import ActorType
from app.models.connection import InstanceConnection
from app.models.instance import Instance
from app.models.knowledge import KnowledgeSource
from app.schemas_api.knowledge import (
    CreateSyncConnectionRequest,
    KnowledgeChunkOut,
    KnowledgeChunkPageOut,
    KnowledgeQuotaOut,
    KnowledgeScopeOut,
    KnowledgeSourceOut,
    OAuthCallbackRequest,
    PasteIngestRequest,
    RenameSourceRequest,
    SaveScopeRequest,
    ScopeCandidateOut,
    SyncConnectionOut,
    SyncResultOut,
)

router = APIRouter(prefix="/api/v1/admin/knowledge", tags=["knowledge"])


def oauth_transport() -> httpx.AsyncClient:
    """Factory for the OAuth token-exchange HTTP transport (§3.2.3/§3.8.3 seam).

    A plain function (not a hardcoded ``httpx.AsyncClient()`` call site) so tests
    can monkeypatch this name to a FAKE async-context-manager transport with no
    real network call — the same pattern ``provider_for_connection`` already
    uses for the read-side sync providers in
    ``tests/contract/test_knowledge_sync_and_edit.py``. Production leaves this
    untouched and gets a real ``httpx.AsyncClient``.
    """
    return httpx.AsyncClient()


# provider (brand) -> source_origin (wire vocabulary, §3.2.3 storage entity note).
# hubspot/salesforce both map to the generic 'crm_kb' origin (Vision §7 "native CRM
# connectors (HubSpot / Salesforce)" is one KB origin, two native connectors).
_PROVIDER_TO_ORIGIN = {
    "google_drive": "google_drive",
    "notion": "notion",
    "hubspot": "crm_kb",
    "salesforce": "crm_kb",
    "website_crawl": "website_crawl",
}


def _source_out(src: KnowledgeSource, used_7d: int = 0) -> KnowledgeSourceOut:
    return KnowledgeSourceOut.model_validate(
        {
            "sourceId": src.id,
            "name": src.name,
            "origin": src.origin,
            "ingestionStatus": src.ingestion_status,
            "sizeBytes": src.size_bytes,
            "lastUpdatedAt": src.last_updated_at,
            "lastSyncedAt": src.last_synced_at,
            "syncStatus": src.sync_status,
            "usedByQuestions7d": used_7d,
            "connectionId": src.connection_id,
            # Round 7 M10: the D10 clock and the tombstone's author, so the dashboard
            # can say "stops answering on <date>" and "deleted at its origin — not
            # restorable" without waiting for a failed restore.
            "pausedSince": src.paused_since,
            "deletedBy": src.deleted_by,
        }
    )


async def _used_by_questions_7d(s: AsyncSession, admin_id: uuid.UUID) -> dict[uuid.UUID, int]:
    """Per-source answer counts over a 7-day rolling window (§3.2.2 tooltip; C14).

    One grouped query over the durable retrieval trace (retrieval_trace_events,
    C6) — the store §5.4 promised the "N questions used this source" confirmation
    would read. Sources absent from the map genuinely served no answer."""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import func

    from app.models.trace import RetrievalTraceEvent

    cutoff = datetime.now(UTC) - timedelta(days=7)
    rows = (
        await s.execute(
            select(RetrievalTraceEvent.source_id, func.count())
            .where(RetrievalTraceEvent.admin_id == admin_id, RetrievalTraceEvent.at >= cutoff)
            .group_by(RetrievalTraceEvent.source_id)
        )
    ).all()
    return {sid: int(count) for sid, count in rows}


async def _instance_id(s: AsyncSession, admin_id: uuid.UUID) -> uuid.UUID:
    inst = (
        await s.execute(select(Instance.id).where(Instance.admin_id == admin_id))
    ).scalar_one_or_none()
    if inst is None:
        raise not_found("No Luciel.")
    return inst


# --- frozen contract methods ------------------------------------------------ #
@router.get("/sources", response_model=list[KnowledgeSourceOut])
async def list_sources(
    response: Response,
    limit: int = Query(SOURCES_MAX, ge=1, le=SOURCES_MAX),
    offset: int = Query(0, ge=0),
    subject: Subject = Depends(current_admin),
) -> list[KnowledgeSourceOut]:
    """Every source the owner can act on, up to SOURCES_MAX; ``X-Total-Count`` says how
    many there are. This page has no pager and is the only place a source can be viewed,
    renamed or deleted, so the ceiling sits well above what a sync adds rather than at
    PAGE_MAX. Past it, the rows removed at origin are cut first, as they sort last."""
    async with tenant_session(subject.admin_id) as s:
        # Round 7 M10: a source the sync found GONE at its origin is listed (with
        # ``deletedBy: "origin"``) until the retention pass purges it, so the owner
        # reads "removed at its source — not restorable" on the list itself instead of
        # discovering it through a failed restore. Owner-deleted tombstones stay off
        # the list (their undo lives on the delete confirmation, unchanged).
        sources = await service.list_sources(
            s, subject.admin_id, include_removed_at_origin=True, limit=limit, offset=offset
        )
        total = await service.count_listed_sources(s, subject.admin_id)
        response.headers[TOTAL_COUNT_HEADER] = str(total)
        used = await _used_by_questions_7d(s, subject.admin_id)
        return [_source_out(x, used.get(x.id, 0)) for x in sources]


@router.get("/sources/{source_id}/chunks", response_model=KnowledgeChunkPageOut)
async def get_chunks(
    source_id: uuid.UUID, subject: Subject = Depends(current_admin)
) -> KnowledgeChunkPageOut:
    """The first page of a source's chunks plus the honest total (round 7 WP-4c #6).

    The list alone made the raw view print "first 20 of 20" for a source with 300
    chunks — the page length was standing in for the count."""
    async with tenant_session(subject.admin_id) as s:
        # view_chunks emits the knowledge_source_viewed audit event (Architecture
        # §3.2.2 "view + edit + delete history flows into admin_audit_log"; Axis4-F04).
        chunks = await service.view_chunks(
            s, subject.admin_id, source_id, subject.user_id, limit=service.CHUNK_PREVIEW_PAGE
        )
        total = await service.count_chunks(s, subject.admin_id, source_id)
        return KnowledgeChunkPageOut(
            chunks=[
                KnowledgeChunkOut.model_validate(
                    {"chunkId": c.id, "sourceId": c.source_id, "ordinal": c.ordinal, "text": c.text}
                )
                for c in chunks
            ],
            total_chunks=total,
            page_size=service.CHUNK_PREVIEW_PAGE,
        )


@router.get("/quota", response_model=KnowledgeQuotaOut)
async def get_quota(subject: Subject = Depends(current_admin)) -> KnowledgeQuotaOut:
    async with tenant_session(subject.admin_id) as s:
        q = await service.quota(s, subject.admin_id)
        return KnowledgeQuotaOut.model_validate(q)


@router.delete("/sources/{source_id}", status_code=204)
async def delete_source(
    source_id: uuid.UUID, response: Response, subject: Subject = Depends(current_admin)
) -> Response:
    async with tenant_session(subject.admin_id) as s:
        ok = await service.delete_source(s, subject.admin_id, source_id, subject.user_id)
    if not ok:
        raise not_found("Source not found.")
    response.status_code = 204
    return response


@router.post("/sources/{source_id}/restore", response_model=KnowledgeSourceOut)
async def restore_source(
    source_id: uuid.UUID, subject: Subject = Depends(current_admin)
) -> KnowledgeSourceOut:
    """Undo a delete inside its 30-day window (§3.2.2, audit F109)."""
    async with tenant_session(subject.admin_id) as s:
        try:
            src = await service.restore_source(s, subject.admin_id, source_id, subject.user_id)
        except service.RemovedAtOrigin as exc:
            # Round 7 WP-4c #9: the sync found this item gone at its origin. That is
            # not the owner's mis-click and the content is no longer at the source,
            # so nothing here can bring it back — say so, not "not in its window".
            gone = exc.source
            raise conflict(
                f"{gone.name} was removed at its sync source "
                f"({gone.source_origin or gone.origin}) and cannot be restored here. "
                "Re-add it at the source and sync again."
            ) from exc
        if src is None:
            raise not_found("Nothing to restore: the source is not in its undo window.")
        used = await _used_by_questions_7d(s, subject.admin_id)
        return _source_out(src, used.get(src.id, 0))


_DEAD_CREDENTIAL_DETAIL = "knowledge sync could not use the stored credential — reconnect"


async def _flag_dead_credential(admin_id: uuid.UUID, connection_id: uuid.UUID) -> None:
    """Bookkeeping for a grant the provider just refused (round 7 WP-4c #7), in its
    OWN transaction.

    The manual routes used to pause the sources inside the request's
    ``tenant_session`` and then raise the 409 — which rolled that transaction back,
    so even the pause was lost — and never touched the connection row, so the
    Configure page kept a green "Connected" chip on a dead credential. The sweep
    does both halves (round 5B item 12: pause + ``mark_connection_broken`` → audit +
    once-per-incident attention email); the routes now do the same, before the 409.
    A row the owner never connected (unconfigured / pending) is paused but NOT
    flipped: "error" would claim a grant it never had.
    """
    from app.connections.health import mark_connection_broken  # noqa: PLC0415

    async with tenant_session(admin_id) as s:
        conn = (
            await s.execute(
                select(InstanceConnection).where(
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.connection_id == connection_id,
                )
            )
        ).scalar_one_or_none()
        if conn is None:
            return
        await sync.pause_sync_on_revocation(s, admin_id=admin_id, connection_id=connection_id)
        if conn.status == "connected":
            await mark_connection_broken(s, conn, detail=_DEAD_CREDENTIAL_DETAIL)


async def _flag_unreachable_site(admin_id: uuid.UUID, connection_id: uuid.UUID) -> None:
    """Bookkeeping for a crawl that reached no page (round 7 Phase 2 review,
    knowledge #2), in its OWN transaction (the 409 unwinds the request's).

    A website crawl holds no credential, so this is NOT ``_flag_dead_credential``:
    the sources read the honest "error" sync state and nothing else changes — no
    D10 stop clock, no pause notice, the connection row keeps its status."""
    async with tenant_session(admin_id) as s:
        await sync.mark_sync_error(s, admin_id=admin_id, connection_id=connection_id)


@router.post("/sources/{source_id}/resync", response_model=KnowledgeSourceOut)
async def resync_source(
    source_id: uuid.UUID,
    subject: Subject = Depends(current_admin),
    confirm_shrink: bool = Query(False, alias="confirmShrink"),
) -> KnowledgeSourceOut:
    """Manual re-sync (Architecture §3.2.3: "the Admin can ... trigger a manual
    re-sync"). For a live-sync source this actually re-pulls via the source's
    connector's ``SyncProvider`` (not just a last_synced_at stamp) — the whole
    connection's scope is reconciled (add/update/remove) so a manual resync behaves
    identically to the scheduled sync worker. For a one-time source (upload/paste/
    csv, no connection_id) this is a no-op refresh: there's nothing to re-pull.
    """
    dead_connection: uuid.UUID | None = None
    try:
        async with tenant_session(subject.admin_id) as s:
            src = (
                await s.execute(
                    select(KnowledgeSource).where(
                        KnowledgeSource.admin_id == subject.admin_id,
                        KnowledgeSource.id == source_id,
                    )
                )
            ).scalar_one_or_none()
            if src is None:
                raise not_found("Source not found.")
            if src.connection_id is not None:
                conn = (
                    await s.execute(
                        select(InstanceConnection).where(
                            InstanceConnection.admin_id == subject.admin_id,
                            InstanceConnection.connection_id == src.connection_id,
                        )
                    )
                ).scalar_one_or_none()
                if conn is None:
                    raise not_found("Connection not found.")
                dead_connection = conn.connection_id
                provider = provider_for_connection(conn, get_secret_store())
                try:
                    await sync.sync_connection(
                        s,
                        admin_id=subject.admin_id,
                        instance_id=src.instance_id,
                        connection_id=conn.connection_id,
                        origin=_PROVIDER_TO_ORIGIN.get(conn.provider, conn.provider),
                        provider=provider,
                        allow_shrink=confirm_shrink,
                    )
                except sync.SnapshotShrunk as exc:
                    # Nothing was deleted (F107); the owner confirms with ?confirmShrink=true.
                    raise conflict(
                        f"The source now reports {exc.remote} item(s) where {exc.existing} are "
                        "synced, so nothing was removed. If you did remove them at the source, "
                        "sync again and confirm the shrink."
                    ) from exc
                refreshed = (
                    await s.execute(
                        select(KnowledgeSource).where(
                            KnowledgeSource.admin_id == subject.admin_id,
                            KnowledgeSource.id == source_id,
                        )
                    )
                ).scalar_one_or_none()
                if refreshed is None:
                    # The source itself was removed by the sync (deleted at origin) —
                    # honest 404 rather than fabricating a stale response (§3.2.3).
                    raise not_found("Source no longer exists at the sync origin.")
                return _source_out(refreshed)
            # One-time ingest: no connector to re-pull from; nothing to do.
            return _source_out(src)
    except CrawlUnreachable as exc:
        # The site answered no page: a failed sync, not a dead credential.
        if dead_connection is not None:
            await _flag_unreachable_site(subject.admin_id, dead_connection)
        raise conflict(f"Sync unavailable: {exc}") from exc
    except ProviderUnavailable as exc:
        # Same clean-error rule as trigger_sync: a revoked/expired/never-connected
        # credential surfaces a structured 409, never a crash. Caught OUTSIDE the
        # request transaction (which the 409 unwinds) so the pause + the honest
        # connection state land in their own (round 7 WP-4c #7).
        if dead_connection is not None:
            await _flag_dead_credential(subject.admin_id, dead_connection)
        raise conflict(f"Sync unavailable: {exc}") from exc


@router.put("/sources/{source_id}", response_model=KnowledgeSourceOut)
async def rename_source(
    source_id: uuid.UUID,
    req: RenameSourceRequest,
    subject: Subject = Depends(current_admin),
) -> KnowledgeSourceOut:
    """Rename affordance (Architecture §3.2.2 raw knowledge view edit affordances:
    "rename source, re-trigger ingestion, or replace the file"). Renaming changes
    only the display name — chunks/embeddings are untouched."""
    async with tenant_session(subject.admin_id) as s:
        src = await service.rename_source(
            s, subject.admin_id, source_id, req.name, actor_user_id=subject.user_id
        )
        if src is None:
            raise not_found("Source not found.")
        return _source_out(src)


@router.post("/sources/{source_id}/replace", response_model=KnowledgeSourceOut)
async def replace_source(
    source_id: uuid.UUID,
    file: UploadFile = File(...),
    subject: Subject = Depends(current_admin),
) -> KnowledgeSourceOut:
    """Replace affordance (§3.2.2): swap the underlying file for an existing source.
    The source row is kept (same id, same place in the list) but its chunks are
    fully replaced by the new file's parsed+chunked+embedded content — this is the
    "re-ingested, chunks updated" flow §5.2/§9 references for correcting a wrong
    answer at its root cause."""
    raw = await file.read()
    try:
        parsed = parse_file(raw, filename=file.filename or "", content_type=file.content_type)
    except ParseError as exc:
        raise validation_error(f"{exc.code}: {exc.message}") from exc
    async with tenant_session(subject.admin_id) as s:
        try:
            src = await service.replace_source(
                s,
                subject.admin_id,
                source_id,
                text=parsed.text,
                size_bytes=len(raw),
                actor_user_id=subject.user_id,
            )
        except QuotaError as exc:
            raise validation_error(f"Knowledge quota: {exc}") from exc
        if src is None:
            raise not_found("Source not found.")
        return _source_out(src)


# --- additive ingest endpoints (not yet in the frozen client) --------------- #
@router.post("/sources/paste", response_model=KnowledgeSourceOut, status_code=201)
async def ingest_paste(
    req: PasteIngestRequest, subject: Subject = Depends(current_admin)
) -> KnowledgeSourceOut:
    async with tenant_session(subject.admin_id) as s:
        instance_id = await _instance_id(s, subject.admin_id)
        try:
            src = await service.ingest_text(
                s,
                admin_id=subject.admin_id,
                instance_id=instance_id,
                name=req.name,
                text=req.text,
                origin="paste",
                actor_user_id=subject.user_id,
            )
        except QuotaError as exc:
            raise validation_error(f"Knowledge quota: {exc}") from exc
        return _source_out(src)


@router.post("/sources/upload", response_model=KnowledgeSourceOut, status_code=201)
async def ingest_upload(
    file: UploadFile = File(...),
    name: str = Form(...),
    subject: Subject = Depends(current_admin),
) -> KnowledgeSourceOut:
    """Upload ingest with real per-format parsing (§3.2.3 file parsers).

    Dispatches by content-type/extension to the pypdf/python-docx/csv/txt
    extractor (``app.knowledge.parsers``) instead of a blind UTF-8 decode, so a
    PDF or DOCX upload is actually readable text before it hits the chunker. A
    CSV upload here is routed the same way ``/sources/csv`` is: origin='csv',
    structured row text. An unparseable file fails cleanly with a structured 422
    (ParseError.code) rather than silently ingesting binary noise."""
    raw = await file.read()
    try:
        parsed = parse_file(raw, filename=file.filename or name, content_type=file.content_type)
    except ParseError as exc:
        raise validation_error(f"{exc.code}: {exc.message}") from exc
    origin = "csv" if parsed.row_count is not None else "upload"
    async with tenant_session(subject.admin_id) as s:
        instance_id = await _instance_id(s, subject.admin_id)
        try:
            src = await service.ingest_text(
                s,
                admin_id=subject.admin_id,
                instance_id=instance_id,
                name=name,
                text=parsed.text,
                origin=origin,
                size_bytes=len(raw),
                actor_user_id=subject.user_id,
            )
        except QuotaError as exc:
            raise validation_error(f"Knowledge quota: {exc}") from exc
        await service.keep_original(
            s,
            source=src,
            data=raw,
            filename=file.filename or name,
            content_type=file.content_type or "application/octet-stream",
        )
        return _source_out(src)


@router.post("/sources/csv", response_model=KnowledgeSourceOut, status_code=201)
async def ingest_csv(
    file: UploadFile = File(...),
    name: str = Form(...),
    subject: Subject = Depends(current_admin),
) -> KnowledgeSourceOut:
    """Explicit CSV import path (Vision §3.3 "CSV/table import"; Architecture
    §3.2.3 storage note: CSV rows are ingested as structured rows/text, not a raw
    blob). Distinct from ``/sources/upload`` so the dashboard's CSV button always
    gets origin='csv' even if the browser sends a generic content-type, and so a
    non-CSV file posted here fails cleanly instead of being silently reinterpreted.
    """
    raw = await file.read()
    try:
        parsed = parse_file(raw, filename=file.filename or name, content_type="text/csv")
    except ParseError as exc:
        raise validation_error(f"{exc.code}: {exc.message}") from exc
    async with tenant_session(subject.admin_id) as s:
        instance_id = await _instance_id(s, subject.admin_id)
        try:
            src = await service.ingest_text(
                s,
                admin_id=subject.admin_id,
                instance_id=instance_id,
                name=name,
                text=parsed.text,
                origin="csv",
                size_bytes=len(raw),
                actor_user_id=subject.user_id,
            )
        except QuotaError as exc:
            raise validation_error(f"Knowledge quota: {exc}") from exc
        await service.keep_original(
            s, source=src, data=raw, filename=file.filename or name, content_type="text/csv"
        )
        return _source_out(src)


# --- live-sync connectors (§3.2.3): create + list + trigger --------------------- #
def _sync_connection_out(c: InstanceConnection) -> SyncConnectionOut:
    return SyncConnectionOut.model_validate(
        {
            "connectionId": c.connection_id,
            "provider": c.provider,
            "status": c.status,
            "nonSecretConfig": c.non_secret_config,
            "statusDetail": c.status_detail,
        }
    )


@router.get("/sync-connections", response_model=list[SyncConnectionOut])
async def list_sync_connections(
    limit: int = Query(PAGE_MAX, ge=1, le=PAGE_MAX),
    offset: int = Query(0, ge=0),
    subject: Subject = Depends(current_admin),
) -> list[SyncConnectionOut]:
    """Non-revoked knowledge_source connections for this tenant (§3.2.3/§3.8), paged.
    Migration 0058 lets a tenant hold many, so the type's unique index no longer caps them."""
    async with tenant_session(subject.admin_id) as s:
        rows = (
            (
                await s.execute(
                    select(InstanceConnection)
                    .where(
                        InstanceConnection.admin_id == subject.admin_id,
                        InstanceConnection.connection_type == "knowledge_source",
                        InstanceConnection.status != "revoked",
                    )
                    .order_by(InstanceConnection.connection_id)
                    .offset(offset)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_sync_connection_out(c) for c in rows]


@router.post("/sync-connections", response_model=SyncConnectionOut, status_code=201)
async def create_sync_connection(
    req: CreateSyncConnectionRequest,
    response: Response,
    subject: Subject = Depends(current_admin),
) -> SyncConnectionOut:
    """Create a knowledge-sync connection (§3.2.3): reuses the Connections layer
    (§3.8) — an ``instance_connections`` row of connection_type='knowledge_source'
    — exactly like a tool/channel connection. For OAuth providers (google_drive,
    notion, crm_kb=HubSpot/Salesforce) this returns an authorizeUrl the admin
    redirects to; for website_crawl (no OAuth) it stores the target URL(s) in
    non_secret_config and is immediately connected — there is no external account
    to authorize, it's just an httpx fetch target (§3.2.3 crawl-sync note).
    """
    if req.provider not in _PROVIDER_TO_ORIGIN:
        raise validation_error(f"Unknown knowledge-sync provider: {req.provider}")
    async with tenant_session(subject.admin_id) as s:
        instance_id = await _instance_id(s, subject.admin_id)
        if req.provider == "website_crawl" and req.crawl_urls:
            # Round 6 WP-D: the same site crawled again is the same connection — every
            # click used to mint a new row, and the pillar had no list to show them.
            wanted = sorted(str(u) for u in req.crawl_urls)
            existing_rows = (
                (
                    await s.execute(
                        select(InstanceConnection).where(
                            InstanceConnection.admin_id == subject.admin_id,
                            InstanceConnection.instance_id == instance_id,
                            InstanceConnection.connection_type == "knowledge_source",
                            InstanceConnection.provider == "website_crawl",
                            InstanceConnection.status != "revoked",
                        )
                    )
                )
                .scalars()
                .all()
            )
            for row in existing_rows:
                raw = (row.non_secret_config or {}).get("crawl_urls")
                have = sorted(str(u) for u in raw) if isinstance(raw, list) else []
                if have != wanted:
                    continue
                if row.status != "connected":
                    # The same site crawled again after "Remove crawl" (disconnect leaves
                    # the row ``not_connected``) or after a failure: the owner just asked
                    # for it, and a crawl has nothing to authorize, so the row is live
                    # again — not handed back dead, where its pages sync but the pillar
                    # never offers Re-sync (found by the first live walk, 2026-10-19).
                    old_status = row.status
                    row.status = "connected"
                    row.status_detail = None
                    await s.flush()
                    await audit.emit(
                        s,
                        admin_id=subject.admin_id,
                        instance_id=instance_id,
                        actor_type=ActorType.admin_user,
                        actor_user_id=subject.user_id,
                        event_type="connection_status_changed",
                        payload={
                            "connection_id": str(row.connection_id),
                            "old_status": old_status,
                            "new_status": "connected",
                            "reason": "crawl_requested_again",
                        },
                    )
                response.status_code = 200
                return _sync_connection_out(row)
        connection, _result = await connections_service.start_connection(
            s,
            admin_id=subject.admin_id,
            instance_id=instance_id,
            connection_type="knowledge_source",
            provider=req.provider,
            user_id=subject.user_id,
        )
        if req.provider == "website_crawl":
            if not req.crawl_urls:
                raise validation_error("website_crawl requires crawl_urls.")
            # Stored as strings: the column is JSONB, and the HttpUrl typing did its
            # work at the boundary.
            connection.non_secret_config = {"crawl_urls": [str(u) for u in req.crawl_urls]}
            connection.status = "connected"  # no OAuth — nothing to authorize
            connection.status_detail = None
            await s.flush()
        return _sync_connection_out(connection)


@router.post("/sync-connections/{connection_id}/sync", response_model=SyncResultOut)
async def trigger_sync(
    connection_id: uuid.UUID,
    subject: Subject = Depends(current_admin),
    confirm_shrink: bool = Query(False, alias="confirmShrink"),
) -> SyncResultOut:
    """Manually trigger a re-sync/crawl for one connection (§3.2.3 "manual
    re-sync ... always available for immediacy"). Reconciles the FULL connected
    scope through the connection's real ``SyncProvider`` — additions, changed-
    revision updates, and source-side deletions all propagate in one call."""
    try:
        async with tenant_session(subject.admin_id) as s:
            conn = (
                await s.execute(
                    select(InstanceConnection).where(
                        InstanceConnection.admin_id == subject.admin_id,
                        InstanceConnection.connection_id == connection_id,
                        InstanceConnection.connection_type == "knowledge_source",
                    )
                )
            ).scalar_one_or_none()
            if conn is None:
                raise not_found("Sync connection not found.")
            if conn.status == "revoked":
                raise conflict("Connection is revoked; reconnect before syncing.")
            instance_id = await _instance_id(s, subject.admin_id)
            # Round 5B item 12: refresh at call time — the manual Re-sync button ran
            # on the raw stored token and failed on an hour-old Google token, landing
            # the sources in a silent pause.
            from app.connections.refresh import ensure_fresh_access_token  # noqa: PLC0415

            await ensure_fresh_access_token(
                s,
                subject.admin_id,
                instance_id,
                connection_type="knowledge_source",
                connection_id=conn.connection_id,
            )
            provider = provider_for_connection(conn, get_secret_store())
            try:
                result = await sync.sync_connection(
                    s,
                    admin_id=subject.admin_id,
                    instance_id=instance_id,
                    connection_id=conn.connection_id,
                    origin=_PROVIDER_TO_ORIGIN.get(conn.provider, conn.provider),
                    provider=provider,
                    allow_shrink=confirm_shrink,
                )
            except sync.SnapshotShrunk as exc:
                # Nothing was deleted (F107). The owner confirms an intentional shrink
                # by re-syncing with ?confirmShrink=true — the frontend offers exactly that.
                raise conflict(
                    f"The source now reports {exc.remote} item(s) where {exc.existing} are "
                    "synced, so nothing was removed. If you did remove them at the source, "
                    "sync again and confirm the shrink."
                ) from exc
            return SyncResultOut.model_validate(result.as_dict())
    except CrawlUnreachable as exc:
        # The site answered no page (round 7 Phase 2 review, knowledge #2): the same
        # 409 the owner always saw, but the bookkeeping is a failed sync — never the
        # revocation pause or the connection flipped to error on a credential-less row.
        await _flag_unreachable_site(subject.admin_id, connection_id)
        raise conflict(f"Sync unavailable: {exc}") from exc
    except ProviderUnavailable as exc:
        # No token on file (never connected / revoked / expired) or the provider's
        # live call isn't reachable in this posture: surface a clean, structured
        # error — never a 500 crash (§3.2.3 "never fabricate freshness"). The pause
        # and the honest connection state are written in their OWN transaction,
        # outside the request's (which this 409 unwinds) — round 7 WP-4c #7: the
        # Configure chip stayed green on a credential the provider had refused.
        await _flag_dead_credential(subject.admin_id, connection_id)
        raise conflict(f"Sync unavailable: {exc}") from exc


# --- knowledge SCOPE selection (Decision #9) ------------------------------------ #
async def _scoped_connection(
    s: AsyncSession, admin_id: uuid.UUID, connection_id: uuid.UUID
) -> InstanceConnection:
    conn = (
        await s.execute(
            select(InstanceConnection).where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.connection_id == connection_id,
                InstanceConnection.connection_type == "knowledge_source",
            )
        )
    ).scalar_one_or_none()
    if conn is None:
        raise not_found("Sync connection not found.")
    return conn


def _scope_out(conn: InstanceConnection) -> KnowledgeScopeOut:
    spec = registry.get_provider(conn.provider)
    selections = knowledge_scope.scope_of(conn).get("selections") or []
    return KnowledgeScopeOut.model_validate(
        {
            "connectionId": conn.connection_id,
            "provider": conn.provider,
            "scopeKind": spec.scope_kind if spec is not None else None,
            "selections": [
                {"id": str(sel["id"]), "name": str(sel.get("name", sel["id"])), "kind": "selected"}
                for sel in selections
                if isinstance(sel, dict) and sel.get("id")
            ],
            "wholeAccount": not knowledge_scope.scope_ids(conn),
        }
    )


@router.get(
    "/sync-connections/{connection_id}/scope/candidates",
    response_model=list[ScopeCandidateOut],
)
async def list_scope_candidates_endpoint(
    connection_id: uuid.UUID, subject: Subject = Depends(current_admin)
) -> list[ScopeCandidateOut]:
    """The folders (Drive) or pages/databases (Notion) the owner may narrow Luciel to
    (Decision #9). Non-secret: ids and display names only (§3.8.3)."""
    async with tenant_session(subject.admin_id) as s:
        conn = await _scoped_connection(s, subject.admin_id, connection_id)
        if conn.status == "revoked":
            raise conflict("Connection is revoked; reconnect before choosing a scope.")
        # Round 5B item 12: same call-time refresh as the sync paths — the scope
        # picker is a live provider call and an hour-old token 401s it.
        from app.connections.refresh import ensure_fresh_access_token  # noqa: PLC0415

        await ensure_fresh_access_token(
            s,
            subject.admin_id,
            await _instance_id(s, subject.admin_id),
            connection_type="knowledge_source",
        )
        transport = oauth_transport()
        try:
            # httpx.AsyncClient's precise stub types are wider than the narrow
            # HttpTransport seam; the calls made through the seam are compatible.
            candidates = await list_scope_candidates(
                conn, get_secret_store(), cast("HttpTransport", transport)
            )
        except ProviderUnavailable as exc:
            # No token on file, or a provider with nothing to narrow — a clean 409/422,
            # never an empty list that reads as "your Drive has no folders".
            raise conflict(f"Scope unavailable: {exc}") from exc
        finally:
            aclose = getattr(transport, "aclose", None)
            if aclose is not None:
                await aclose()
        return [ScopeCandidateOut.model_validate(c) for c in candidates]


@router.get("/sync-connections/{connection_id}/scope", response_model=KnowledgeScopeOut)
async def get_scope(
    connection_id: uuid.UUID, subject: Subject = Depends(current_admin)
) -> KnowledgeScopeOut:
    async with tenant_session(subject.admin_id) as s:
        return _scope_out(await _scoped_connection(s, subject.admin_id, connection_id))


@router.put("/sync-connections/{connection_id}/scope", response_model=KnowledgeScopeOut)
async def save_scope(
    connection_id: uuid.UUID,
    req: SaveScopeRequest,
    subject: Subject = Depends(current_admin),
) -> KnowledgeScopeOut:
    """Save which folders/pages Luciel may read (Decision #9).

    The scope lands in the connection's ``non_secret_config`` — a list of ids is a POINTER,
    not a credential, so this adds no secret surface (§3.8.3). It takes effect on the NEXT
    sync; already-ingested out-of-scope sources are removed by that sync's own deletion
    propagation, because the narrowed snapshot no longer contains them.

    An empty ``selections`` list CLEARS the scope, meaning the whole connected account."""
    async with tenant_session(subject.admin_id) as s:
        conn = await _scoped_connection(s, subject.admin_id, connection_id)
        spec = registry.get_provider(conn.provider)
        if spec is None or spec.scope_kind is None:
            raise validation_error(f"'{conn.provider}' has no selectable scope.")
        knowledge_scope.write_scope(
            conn,
            kind=spec.scope_kind,
            selections=[{"id": sel.id, "name": sel.name or sel.id} for sel in req.selections],
        )
        instance_id = await _instance_id(s, subject.admin_id)
        await s.flush()
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            instance_id=instance_id,
            actor_type=ActorType.admin_user,
            actor_user_id=subject.user_id,
            event_type="knowledge_source_scope_changed",
            payload={
                "connection_id": str(conn.connection_id),
                "provider": conn.provider,
                "scope_kind": spec.scope_kind,
                "selected_ids": knowledge_scope.scope_ids(conn),
            },
        )
        return _scope_out(conn)


@router.post("/sync-connections/{connection_id}/oauth-callback", response_model=SyncConnectionOut)
async def oauth_callback(
    connection_id: uuid.UUID,
    req: OAuthCallbackRequest,
    subject: Subject = Depends(current_admin),
) -> SyncConnectionOut:
    """Complete the OAuth connect flow (§3.2.3/§3.8.3): exchange the provider's
    ``code`` for an access token and write it to the SecretStore as this
    connection's tenant-scoped secret_ref, then mark the connection connected.

    This is the link that was missing between ``start_connection``'s
    authorizeUrl and the sync engine's ``_bearer_token`` read: without it, a
    live-sync OAuth connection could never leave ``unconfigured`` because
    nothing ever wrote a token to the SecretStore. The provider's own consent
    screen and code issuance is the one step this endpoint cannot exercise
    without a real click-through (see gap_oauth.md) — everything downstream of
    receiving a ``code`` is real code, exercised in tests against a FAKE
    HttpTransport per the Build Brief's no-real-network-call rule.

    The ``state`` is MANDATORY and must be the single-use one this connection's
    connect-start minted (§5.1). Without that check the endpoint would redeem a code
    obtained from any site that could drive the admin's browser here — CSRF straight
    into a tenant's connection. Redeeming clears the pending attempt, so a replayed
    callback finds nothing to complete.

    A revoked/expired/invalid code is surfaced as a clean 409 (never a crash).
    """
    async with tenant_session(subject.admin_id) as s:
        conn = (
            await s.execute(
                select(InstanceConnection).where(
                    InstanceConnection.admin_id == subject.admin_id,
                    InstanceConnection.connection_id == connection_id,
                    InstanceConnection.connection_type == "knowledge_source",
                )
            )
        ).scalar_one_or_none()
        if conn is None:
            raise not_found("Sync connection not found.")
        # Registry truth, not a literal tuple (round 4): the old hardcoded
        # ("google_drive", "hubspot", "salesforce") gate silently rejected notion on
        # THIS surface while the browser callback accepted it — the two completion
        # surfaces must always agree on which providers are OAuth-exchangeable.
        provider_spec = registry.get_provider(conn.provider)
        if provider_spec is None or not provider_spec.is_oauth:
            raise validation_error(
                f"Provider '{conn.provider}' has no OAuth callback (not an OAuth-class provider)."
            )
        # Single-use: read-and-clear BEFORE the exchange, so a code replayed against a
        # concurrent/retried callback cannot find a live attempt to ride.
        expected_jti = connections_service.consume_oauth_state(conn)
        try:
            oauth_flow.verify_state(
                req.state,
                admin_id=subject.admin_id,
                connection_id=conn.connection_id,
                expected_jti=expected_jti,
            )
        except oauth_flow.StateRejected as exc:
            await s.flush()
            raise validation_error(str(exc)) from exc

        spec = oauth_flow.authorize_spec(conn.provider)
        verifier = (
            oauth_flow.verifier_for_jti(expected_jti)
            if spec is not None and spec.supports_pkce and expected_jti
            else None
        )

        instance_id = await _instance_id(s, subject.admin_id)
        store = get_secret_store()
        async with oauth_transport() as transport:
            try:
                # httpx.AsyncClient structurally satisfies HttpTransport at runtime
                # (get/post); cast for the type checker (the Protocol is intentionally
                # narrower than AsyncClient's full signature).
                material = await exchange_code_for_token(
                    conn.provider,
                    code=req.code,
                    transport=cast("HttpTransport", transport),
                    code_verifier=verifier,
                )
            except OAuthExchangeError as exc:
                conn.status_detail = f"oauth_exchange_failed: {exc}"
                await s.flush()
                raise conflict(f"OAuth token exchange failed: {exc}") from exc
            # Round 6 WP-C: a knowledge grant is verified like every other provider —
            # Drive must carry the read scope, Notion must answer as its bot — before
            # the token is stored or the row flips. A refusal leaves the row where it
            # was, with the provider's reason on it.
            verify_updates: dict[str, object] | None = None
            provider_verifier = POST_EXCHANGE_VERIFIERS.get(conn.provider)
            if provider_verifier is not None:
                try:
                    verify_updates = await provider_verifier(
                        material=material, transport=cast("HttpTransport", transport)
                    )
                except PostExchangeVerifyFailed as exc:
                    conn.status_detail = f"verify_failed: {exc}"
                    await s.flush()
                    raise conflict(str(exc)) from exc
        await store_oauth_token(
            store,
            admin_id=subject.admin_id,
            instance_id=instance_id,
            material=material,
            secret_ref=conn.secret_ref or None,  # this row's own ref (F092)
        )
        old_status = conn.status
        conn.status = "connected"
        conn.status_detail = None
        if verify_updates:
            cfg = dict(conn.non_secret_config or {})
            cfg.update(verify_updates)
            conn.non_secret_config = cfg
        await s.flush()
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            instance_id=instance_id,
            actor_type=ActorType.admin_user,
            actor_user_id=subject.user_id,
            event_type="connection_status_changed",
            payload={
                "connection_id": str(conn.connection_id),
                "old_status": old_status,
                "new_status": "connected",
                "reason": "oauth_callback_token_exchanged",
            },
        )
        return _sync_connection_out(conn)
