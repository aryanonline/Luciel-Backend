"""/api/v1/admin/account/* — export + close (Architecture §3.6.6, §5.10)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Subject, current_admin
from app.api.errors import conflict
from app.audit import log as audit
from app.config import get_settings
from app.db.session import tenant_session
from app.lifecycle import cascade
from app.lifecycle.export_bundle import build_export_zip
from app.lifecycle.export_link import export_download_url
from app.lifecycle.export_store import ExportStore, export_key_for, get_export_store
from app.models.audit import ActorType
from app.models.instance import Account, Instance, LucielState
from app.notifications import email_dispatcher
from app.schemas_api.common import WireModel

router = APIRouter(prefix="/api/v1/admin/account", tags=["account"])


class CloseAccountRequest(WireModel):
    """POST /admin/account/close body (round 6 WP-D, audit F156).

    Closing subsumes deleting the Luciel (§3.6.6). A Luciel that is still serving is
    not deleted silently: the body must say so, or the caller deletes it first."""

    confirm_delete_luciel: bool = False


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
    await store.put(key=key, data=zip_bytes)
    # The 7-day life is the TOKEN's (audit F028): a presigned S3 URL dies with the
    # task-role credentials that signed it, within hours. The link the owner gets
    # points at the API, which mints a fresh minutes-long presign on every visit
    # for as long as the token verifies.
    url = export_download_url(admin_id, export_id, ttl_days=settings.export_link_ttl_days)
    payload: dict[str, object] = {
        "size_bytes": len(zip_bytes),
        "export_id": str(export_id),
        "ttl_days": settings.export_link_ttl_days,
        # Deliberately NO download_url here: the audit log is append-only, 1-year
        # retained, AND included in every subsequent export bundle — persisting the
        # presigned link there kept a working, unauthenticated door to a full tenant
        # data dump readable long after (and far more widely than) its 7-day life
        # warranted. The link goes to the owner via the API response + email below;
        # the audit row records THAT an export was minted (export_id), never the key
        # to open it.
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
    req: CloseAccountRequest | None = None,
    subject: Subject = Depends(current_admin),
    store: ExportStore = Depends(get_export_store),
) -> Response:
    """Close the account — export-first, deletes the Luciel first if present (§3.6.6).

    Round 6 WP-D (F156): a Luciel that is still active or paused is deleted by the
    closure, so the request must carry ``confirmDeleteLuciel: true``; otherwise 409
    names the step (delete the Luciel first, or confirm)."""
    async with tenant_session(subject.admin_id) as s:
        inst = (
            await s.execute(select(Instance).where(Instance.admin_id == subject.admin_id))
        ).scalar_one_or_none()
        if (
            inst is not None
            and inst.state in (LucielState.active, LucielState.paused)
            and not (req and req.confirm_delete_luciel)
        ):
            raise conflict(
                "Your Luciel is still active. Delete it first, or confirm that closing the "
                "account deletes it."
            )
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
