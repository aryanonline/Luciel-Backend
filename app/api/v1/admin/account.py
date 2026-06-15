"""/api/v1/admin/account/* — export + close (Architecture §3.6.6, §5.10)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from app.api.deps import Subject, current_admin
from app.audit import log as audit
from app.db.session import tenant_session
from app.lifecycle import cascade
from app.lifecycle.export_bundle import build_export_zip
from app.models.audit import ActorType
from app.schemas_api.auth import OkResult

router = APIRouter(prefix="/api/v1/admin/account", tags=["account"])


@router.post("/export", response_model=OkResult)
async def request_export(subject: Subject = Depends(current_admin)) -> OkResult:
    """Self-serve export (all accounts, any time — §5.10).

    Builds the bundle (in prod: upload to S3 + issue a 7-day presigned URL) and
    logs the request. Returns ok; the link is emailed/surfaced asynchronously.
    """
    async with tenant_session(subject.admin_id) as s:
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            actor_user_id=subject.user_id,
            actor_type=ActorType.admin_user,
            event_type="data_export_self_serve",
        )
        # Build the bundle to prove it assembles (discarded here; persisted to S3 in prod).
        _zip = await build_export_zip(s, subject.admin_id)
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            actor_user_id=subject.user_id,
            actor_type=ActorType.platform,
            event_type="data_export_ready",
            payload={"size_bytes": len(_zip)},
        )
    return OkResult(ok=True)


@router.post("/close", status_code=204)
async def close_account(response: Response, subject: Subject = Depends(current_admin)) -> Response:
    """Close the account — export-first, deletes the Luciel first if present (§3.6.6)."""
    async with tenant_session(subject.admin_id) as s:
        # Export-first: assemble a bundle before destruction (prod: persist + email link).
        _zip = await build_export_zip(s, subject.admin_id)
        await audit.emit(
            s,
            admin_id=subject.admin_id,
            actor_user_id=subject.user_id,
            actor_type=ActorType.platform,
            event_type="data_export_ready",
            payload={"size_bytes": len(_zip), "context": "account_close"},
        )
        await cascade.close_account(s, subject.admin_id, subject.user_id)
    response.status_code = 204
    return response
