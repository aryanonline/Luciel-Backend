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
from app.api.errors import not_found, validation_error
from app.db.session import tenant_session
from app.knowledge import service
from app.knowledge.service import QuotaError
from app.models.instance import Instance
from app.models.knowledge import KnowledgeSource
from app.schemas_api.knowledge import (
    KnowledgeChunkOut,
    KnowledgeQuotaOut,
    KnowledgeSourceOut,
    PasteIngestRequest,
)

router = APIRouter(prefix="/api/v1/admin/knowledge", tags=["knowledge"])


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
    # Manual re-sync (live-sync sources). For one-time sources this is a no-op refresh.
    # Full sync mechanics land in Arc 11b; here it stamps last_synced_at for synced sources.
    from datetime import UTC, datetime

    from app.models.knowledge import KnowledgeSource

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
        if src.sync_status is not None:
            src.last_synced_at = datetime.now(UTC)
            src.sync_status = "synced"
            await s.flush()
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
    raw = await file.read()
    # Text extraction for binary formats is a pluggable stage; here we decode text.
    try:
        text = raw.decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        text = ""
    async with tenant_session(subject.admin_id) as s:
        instance_id = await _instance_id(s, subject.admin_id)
        try:
            src = await service.ingest_text(
                s,
                admin_id=subject.admin_id,
                instance_id=instance_id,
                name=name,
                text=text,
                origin="upload",
                size_bytes=len(raw),
                actor_user_id=subject.user_id,
            )
        except QuotaError as exc:
            raise validation_error(f"Knowledge quota: {exc}") from exc
        return _source_out(src)
