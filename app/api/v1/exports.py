"""GET /api/v1/exports/{token} — the account-export download door (§3.6.6, F028).

Unauthenticated by session on purpose: the link arrives by email and is opened from
wherever the owner reads mail. The signed token IS the authorization — it names the
tenant and the export, expires after ``export_link_ttl_days``, and was minted only by
the export flow for that tenant. A valid token redirects to a fresh, minutes-long
presigned object URL; anything else is a plain 410 Gone with owner-facing copy (no
oracle on which tokens exist — invalid and expired read the same).
"""

from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.api.errors import ApiError, rate_limited
from app.audit import log as audit
from app.channels import rate_limit
from app.db.session import tenant_session
from app.lifecycle.export_link import PRESIGN_TTL_SECONDS, ExportLinkRejected, parse_export_token
from app.lifecycle.export_store import export_key_for, get_export_store
from app.models.audit import ActorType
from app.models.instance import Account

router = APIRouter(prefix="/api/v1/exports", tags=["exports"])
_log = logging.getLogger(__name__)

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
    # Per link, before any tenant work (adversarial review R-WP2-2): a valid token
    # replayed in a loop must not write an audit row and mint a presign per hit.
    link_key = hashlib.sha256(token.encode()).hexdigest()[:32]
    if not rate_limit.get_store().check_source("export_download", link_key).allowed:
        raise rate_limited(
            "This link was opened too many times in a row — wait a minute and try again.",
            retry_after_seconds=60,
        )
    async with tenant_session(admin_id) as s:
        acct = (await s.execute(select(Account).where(Account.id == admin_id))).scalar_one_or_none()
        if acct is None:
            # Purged tenant: the day-30 cascade swept ``vm-export-bundles/{admin_id}/``
            # along with the account row (round 7 WP-5, item 4), so there is no object
            # to presign and no tenant to audit under. The link must not say anything
            # more specific than "expired" — no oracle on which tenants ever existed.
            raise ApiError(410, _EXPIRED)
        store = get_export_store()
        key = export_key_for(admin_id, export_id)
        try:
            present = await store.exists(key=key)
        except Exception:  # noqa: BLE001 — a store that cannot answer is not "gone"
            # Fall through to the presign, as before this check existed: the object
            # is very likely there and the redirect is what tells the owner the truth.
            _log.warning("export_exists_check_failed admin_id=%s", admin_id, exc_info=True)
            present = True
        if not present:
            # The account outlived its bundle (the object is gone — e.g. removed by
            # hand, or the bucket's lifecycle backstop ran early). A Luciel hard delete
            # no longer sweeps bundles: only the account purge does, and that removes
            # the account too (round 7 money review, finding 1). The token still
            # verifies, but a presign would redirect to the bucket's raw NoSuchKey page
            # (round 7 WP-5, item 8).
            raise ApiError(410, _EXPIRED)
        url = await store.presign(key=key, ttl_seconds=PRESIGN_TTL_SECONDS)
        await audit.emit(
            s,
            admin_id=admin_id,
            actor_type=ActorType.admin_user,
            event_type="data_export_downloaded",
            payload={"export_id": str(export_id), "presign_ttl_seconds": PRESIGN_TTL_SECONDS},
        )
    return RedirectResponse(url=url, status_code=302)
