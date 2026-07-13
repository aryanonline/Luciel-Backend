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

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Subject, current_admin
from app.api.errors import conflict, not_found, validation_error
from app.connections import service as connections_service
from app.connections.secrets import get_secret_store
from app.db.session import tenant_session
from app.knowledge import service, sync
from app.knowledge.parsers import ParseError, parse_file
from app.knowledge.providers import provider_for_connection
from app.knowledge.service import QuotaError
from app.models.connection import InstanceConnection
from app.models.instance import Instance
from app.models.knowledge import KnowledgeSource
from app.schemas_api.knowledge import (
    CreateSyncConnectionRequest,
    KnowledgeChunkOut,
    KnowledgeQuotaOut,
    KnowledgeSourceOut,
    PasteIngestRequest,
    RenameSourceRequest,
    SyncConnectionOut,
    SyncResultOut,
)

router = APIRouter(prefix="/api/v1/admin/knowledge", tags=["knowledge"])

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


def _source_out(src: KnowledgeSource) -> KnowledgeSourceOut:
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
        }
    )


async def _instance_id(s: AsyncSession, admin_id: uuid.UUID) -> uuid.UUID:
    inst = (
        await s.execute(select(Instance.id).where(Instance.admin_id == admin_id))
    ).scalar_one_or_none()
    if inst is None:
        raise not_found("No Luciel.")
    return inst


# --- frozen contract methods ------------------------------------------------ #
@router.get("/sources", response_model=list[KnowledgeSourceOut])
async def list_sources(subject: Subject = Depends(current_admin)) -> list[KnowledgeSourceOut]:
    async with tenant_session(subject.admin_id) as s:
        sources = await service.list_sources(s, subject.admin_id)
        return [_source_out(x) for x in sources]


@router.get("/sources/{source_id}/chunks", response_model=list[KnowledgeChunkOut])
async def get_chunks(
    source_id: uuid.UUID, subject: Subject = Depends(current_admin)
) -> list[KnowledgeChunkOut]:
    async with tenant_session(subject.admin_id) as s:
        # view_chunks emits the knowledge_source_viewed audit event (Architecture
        # §3.2.2 "view + edit + delete history flows into admin_audit_log"; Axis4-F04).
        chunks = await service.view_chunks(s, subject.admin_id, source_id, subject.user_id)
        return [
            KnowledgeChunkOut.model_validate(
                {"chunkId": c.id, "sourceId": c.source_id, "ordinal": c.ordinal, "text": c.text}
            )
            for c in chunks
        ]


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


@router.post("/sources/{source_id}/resync", response_model=KnowledgeSourceOut)
async def resync_source(
    source_id: uuid.UUID, subject: Subject = Depends(current_admin)
) -> KnowledgeSourceOut:
    """Manual re-sync (Architecture §3.2.3: "the Admin can ... trigger a manual
    re-sync"). For a live-sync source this actually re-pulls via the source's
    connector's ``SyncProvider`` (not just a last_synced_at stamp) — the whole
    connection's scope is reconciled (add/update/remove) so a manual resync behaves
    identically to the scheduled sync worker. For a one-time source (upload/paste/
    csv, no connection_id) this is a no-op refresh: there's nothing to re-pull.
    """
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
            provider = provider_for_connection(conn, get_secret_store())
            await sync.sync_connection(
                s,
                admin_id=subject.admin_id,
                instance_id=src.instance_id,
                connection_id=conn.connection_id,
                origin=_PROVIDER_TO_ORIGIN.get(conn.provider, conn.provider),
                provider=provider,
            )
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
    subject: Subject = Depends(current_admin),
) -> list[SyncConnectionOut]:
    """All non-revoked knowledge_source connections for this tenant (§3.2.3/§3.8)."""
    async with tenant_session(subject.admin_id) as s:
        rows = (
            (
                await s.execute(
                    select(InstanceConnection).where(
                        InstanceConnection.admin_id == subject.admin_id,
                        InstanceConnection.connection_type == "knowledge_source",
                        InstanceConnection.status != "revoked",
                    )
                )
            )
            .scalars()
            .all()
        )
        return [_sync_connection_out(c) for c in rows]


@router.post("/sync-connections", response_model=SyncConnectionOut, status_code=201)
async def create_sync_connection(
    req: CreateSyncConnectionRequest, subject: Subject = Depends(current_admin)
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
            connection.non_secret_config = {"crawl_urls": req.crawl_urls}
            connection.status = "connected"  # no OAuth — nothing to authorize
            connection.status_detail = None
            await s.flush()
        return _sync_connection_out(connection)


@router.post("/sync-connections/{connection_id}/sync", response_model=SyncResultOut)
async def trigger_sync(
    connection_id: uuid.UUID, subject: Subject = Depends(current_admin)
) -> SyncResultOut:
    """Manually trigger a re-sync/crawl for one connection (§3.2.3 "manual
    re-sync ... always available for immediacy"). Reconciles the FULL connected
    scope through the connection's real ``SyncProvider`` — additions, changed-
    revision updates, and source-side deletions all propagate in one call."""
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
        provider = provider_for_connection(conn, get_secret_store())
        result = await sync.sync_connection(
            s,
            admin_id=subject.admin_id,
            instance_id=instance_id,
            connection_id=conn.connection_id,
            origin=_PROVIDER_TO_ORIGIN.get(conn.provider, conn.provider),
            provider=provider,
        )
        return SyncResultOut.model_validate(
            {"added": result.added, "updated": result.updated, "removed": result.removed}
        )
