"""/api/v1/admin/account/* — export + close (Architecture §3.6.6, §5.10)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Subject, current_admin
from app.audit import log as audit
from app.config import get_settings
from app.db.session import tenant_session
from app.lifecycle import cascade
from app.lifecycle.export_bundle import build_export_zip
from app.lifecycle.export_store import ExportStore, export_key_for, get_export_store
from app.models.audit import ActorType
from app.models.instance import Account
from app.notifications import email_dispatcher

router = APIRouter(prefix="/api/v1/admin/account", tags=["account"])


class ExportResult(BaseModel):
    """Export response — carries the time-limited download link so the customer can
    actually retrieve their bundle (§5.10). Also emailed to the account owner."""

    ok: bool = True
    download_url: str
    ttl_days: int


async def _build_upload_and_notify(
    s: AsyncSession,
    *,
    admin_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    store: ExportStore,
    context: str | None = None,
) -> str:
    """Build the export ZIP, upload it, and emit ``data_export_ready`` with the
    presigned link (§3.6.6 / §5.10). Shared by ``request_export`` and
    ``close_account`` so both paths persist the bundle instead of discarding it.

    Returns the presigned download URL."""
    settings = get_settings()
    zip_bytes = await build_export_zip(s, admin_id)
    export_id = uuid.uuid4()
    key = export_key_for(admin_id, export_id)
    url = await store.put_and_presign(
        key=key, data=zip_bytes, ttl_days=settings.export_link_ttl_days
    )
    payload: dict[str, object] = {
        "size_bytes": len(zip_bytes),
        "export_id": str(export_id),
        "ttl_days": settings.export_link_ttl_days,
        # The presigned URL itself is not secret material (§3.8.3 covers connection
        # credentials, not this time-limited download link), but it IS sensitive —
        # surfaced via audit payload the same way other lifecycle links are, so the
        # admin dashboard/notification path can read it back.
        "download_url": url,
    }
    if context is not None:
        payload["context"] = context
    await audit.emit(
        s,
        admin_id=admin_id,
        actor_user_id=actor_user_id,
        actor_type=ActorType.platform,
        event_type="data_export_ready",
        payload=payload,
    )
    # Deliver the link to the account owner by email (§5.10 — the admin receives the
    # time-limited link). Degrades to audit-only if email isn't configured.
    owner_email = (
        await s.execute(select(Account.email).where(Account.id == admin_id))
    ).scalar_one_or_none()
    if owner_email:
        await email_dispatcher.send_export_ready(
            s,
            admin_id=admin_id,
            to=owner_email,
            download_url=url,
            ttl_days=settings.export_link_ttl_days,
        )
    return url


@router.post("/export", response_model=ExportResult)
async def request_export(
    subject: Subject = Depends(current_admin),
    store: ExportStore = Depends(get_export_store),
) -> ExportResult:
    """Self-serve export (all accounts, any time — §5.10).

    Builds the bundle, uploads it to the ExportStore (S3 ``vm-export-bundles`` in
    live mode), and issues a 7-day presigned URL (§3.6.6 ``export_link_ttl_days``).
    Logs the request AND the ready event (with the link) — previously the ZIP was
    built and discarded; now it is actually persisted and reachable.
    """
    async with tenant_session(subject.admin_id) as s:
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            actor_user_id=subject.user_id,
            actor_type=ActorType.admin_user,
            event_type="data_export_self_serve",
        )
        url = await _build_upload_and_notify(
            s, admin_id=subject.admin_id, actor_user_id=subject.user_id, store=store
        )
        ttl = get_settings().export_link_ttl_days
    return ExportResult(ok=True, download_url=url, ttl_days=ttl)


@router.post("/close", status_code=204)
async def close_account(
    response: Response,
    subject: Subject = Depends(current_admin),
    store: ExportStore = Depends(get_export_store),
) -> Response:
    """Close the account — export-first, deletes the Luciel first if present (§3.6.6)."""
    async with tenant_session(subject.admin_id) as s:
        # Export-first: assemble + persist a bundle, issue the link, before destruction.
        await _build_upload_and_notify(
            s,
            admin_id=subject.admin_id,
            actor_user_id=subject.user_id,
            store=store,
            context="account_close",
        )
        await cascade.close_account(s, subject.admin_id, subject.user_id)
    response.status_code = 204
    return response
