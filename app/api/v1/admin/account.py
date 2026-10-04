"""/api/v1/admin/account/* — status, export, close (Architecture §3.6.6, §5.10).

Owner decision D5 (2026-09-14): for 30 days after closure the owner can sign in to a
REDUCED surface — read the account status, reopen it, re-request the export; nothing
else. Those routes depend on ``current_account_owner``; ``close`` (like every other
admin route) stays on ``current_admin`` and refuses a closed account with the typed
403 ``account_closed``.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Subject, current_account_owner, current_admin
from app.api.errors import conflict, unauthorized
from app.audit import log as audit
from app.config import get_settings
from app.db.session import tenant_session
from app.lifecycle import cascade
from app.lifecycle.close_purge import closure_started_at, reopen_deadline
from app.lifecycle.export_bundle import build_export_zip, hold_export_lock
from app.lifecycle.export_link import export_download_url
from app.lifecycle.export_store import ExportStore, export_key_for, get_export_store
from app.models.audit import ActorType
from app.models.instance import Account, AccountState, Instance, LucielState
from app.notifications import email_dispatcher
from app.schemas_api.account import AccountStatusOut
from app.schemas_api.common import WireModel

router = APIRouter(prefix="/api/v1/admin/account", tags=["account"])
_log = logging.getLogger(__name__)


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
    # What happened to the email carrying the same link (round 7 WP-5, item 4):
    # ``sent`` | ``recorded`` (mock/CI: the Null sender took it, nothing left the
    # platform) | ``no_sender`` (live with no platform sender identity bound — the
    # link went nowhere) | ``failed:<ExceptionClass>``. The dispatcher's return value
    # used to be dropped, so the UI said "check your inbox" over a mail that never left.
    email_outcome: str = Field(serialization_alias="emailOutcome")


def _email_outcome(outcome: email_dispatcher.DeliveryOutcome) -> str:
    if outcome.status == "failed":
        return f"failed:{outcome.reason or 'unknown'}"
    if outcome.status == "sent":
        return "sent"
    # "recorded": the Null sender took it. In live that means no platform sender
    # identity is bound (the link went nowhere); in mock/CI it is the expected posture.
    return "no_sender" if outcome.degraded else "recorded"


async def _build_upload_and_notify(
    s: AsyncSession,
    *,
    admin_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    store: ExportStore,
    context: str | None = None,
) -> tuple[str, str]:
    """Build the export ZIP, upload it, and emit ``data_export_ready`` with the
    presigned link (§3.6.6 / §5.10). Shared by ``request_export`` and
    ``close_account`` so both paths persist the bundle instead of discarding it.

    Returns the download URL and the email outcome word (see ``ExportResult``).

    Serialized with the hard-delete cascade (round 7 WP-5, item 8): the lock is taken
    before the build, so a day-30 purge either waits for this bundle and sweeps it, or
    finishes first — and then the account is re-read, because a purged tenant has
    nothing to export and nobody to hand a link to. That refusal is a 401 (the session
    names an account that no longer exists), never a "ready" over an empty bundle."""
    settings = get_settings()
    await hold_export_lock(s, admin_id)
    owner_email = (
        await s.execute(select(Account.email).where(Account.id == admin_id))
    ).scalar_one_or_none()
    if owner_email is None:
        raise unauthorized("Account not found.")
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
    outcome = await email_dispatcher.send_export_ready(
        s,
        admin_id=admin_id,
        to=owner_email,
        download_url=url,
        ttl_days=settings.export_link_ttl_days,
    )
    email_outcome = _email_outcome(outcome)
    if email_outcome not in ("sent", "recorded"):
        # The closure path answers 204 and has no body to carry this; the log line is
        # what an operator sees. The owner still has the D5 re-request (item 5).
        _log.warning(
            "export_ready_email_not_delivered admin_id=%s context=%s outcome=%s",
            admin_id,
            context or "self_serve",
            email_outcome,
        )
    return url, email_outcome


@router.get("", response_model=AccountStatusOut)
async def get_account(subject: Subject = Depends(current_account_owner)) -> AccountStatusOut:
    """The account's lifecycle status — the one READ a closed account may make (D5).

    For a closed account: when it closed, until when it can be reopened (the same
    deadline the day-30 purge uses), and whether reopening brings the Luciel back.
    For an open account the instants are null — the frontend asks one place either way.
    """
    async with tenant_session(subject.admin_id) as s:
        acct = (await s.execute(select(Account).where(Account.id == subject.admin_id))).scalar_one()
        if acct.state != AccountState.closed:
            return AccountStatusOut(state=acct.state.value)
        closed_at = await closure_started_at(s, subject.admin_id, account=acct)
        reopen_until = reopen_deadline(closed_at) if closed_at is not None else None
        inst = (
            await s.execute(select(Instance).where(Instance.admin_id == subject.admin_id))
        ).scalar_one_or_none()
        record = await cascade.closure_record(s, subject.admin_id)
    return AccountStatusOut(
        state=acct.state.value,
        closed_at=closed_at,
        reopen_until=reopen_until,
        can_reopen=reopen_until is not None and datetime.now(UTC) < reopen_until,
        luciel_restored_on_reopen=(
            record.get("luciel_deleted_by_closure") is True
            and inst is not None
            and inst.state == LucielState.luciel_grace_window
        ),
    )


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
        url, email_outcome = await _build_upload_and_notify(
            s, admin_id=subject.admin_id, actor_user_id=subject.user_id, store=store
        )
        ttl = get_settings().export_link_ttl_days
    return ExportResult(ok=True, download_url=url, ttl_days=ttl, email_outcome=email_outcome)


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
