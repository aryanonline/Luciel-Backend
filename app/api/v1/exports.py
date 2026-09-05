"""GET /api/v1/exports/{token} — the account-export download door (§3.6.6, F028).

Unauthenticated by session on purpose: the link arrives by email and is opened from
wherever the owner reads mail. The signed token IS the authorization — it names the
tenant and the export, expires after ``export_link_ttl_days``, and was minted only by
the export flow for that tenant. A valid token redirects to a fresh, minutes-long
presigned object URL; anything else is a plain 410 Gone with owner-facing copy (no
oracle on which tokens exist — invalid and expired read the same).
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.api.errors import ApiError
from app.audit import log as audit
from app.db.session import tenant_session
from app.lifecycle.export_link import PRESIGN_TTL_SECONDS, ExportLinkRejected, parse_export_token
from app.lifecycle.export_store import export_key_for, get_export_store
from app.models.audit import ActorType
from app.models.instance import Account

router = APIRouter(prefix="/api/v1/exports", tags=["exports"])

_EXPIRED = (
    "This export link has expired or is not valid. Request a new export from the "
    "Account page of your dashboard."
)


@router.get("/{token}", status_code=302)
async def download_export(token: str) -> RedirectResponse:
    try:
        admin_id, export_id = parse_export_token(token)
    except ExportLinkRejected as exc:
        raise ApiError(410, _EXPIRED) from exc
    async with tenant_session(admin_id) as s:
        acct = (await s.execute(select(Account).where(Account.id == admin_id))).scalar_one_or_none()
        if acct is None:
            # Purged tenant: the object is gone with the account; the link must not
            # say anything more specific than "expired".
            raise ApiError(410, _EXPIRED)
        url = await get_export_store().presign(
            key=export_key_for(admin_id, export_id), ttl_seconds=PRESIGN_TTL_SECONDS
        )
        await audit.emit(
            s,
            admin_id=admin_id,
            actor_type=ActorType.admin_user,
            event_type="data_export_downloaded",
            payload={"export_id": str(export_id), "presign_ttl_seconds": PRESIGN_TTL_SECONDS},
        )
    return RedirectResponse(url=url, status_code=302)
