"""Day-30 grace-window expiry sweep (Architecture §3.6.3 / §3.6.5).

When a Luciel is soft-deleted it enters ``luciel_grace_window`` with
``grace_window_started_at`` stamped. The grace promise (§3.6.3, Legal §A4/§B5) is
that the data is recoverable for 30 days and then PERMANENTLY destroyed. This
module is the platform job that honors the "then destroyed" half: at day 30 it
fires the hard-delete cascade (§3.6.5) so data committed for deletion is not
retained indefinitely.

Shape mirrors ``connections/health.run_health_sweep``: a per-tenant one-shot that
CI/tests call directly, plus a thin multi-tenant driver that iterates a set of
candidate admin_ids. The cross-tenant DISCOVERY of which tenants are due (a
platform-role query over ``instances``, which is RLS-FORCEd) and the periodic
schedule that calls this are the infra/ops half — wired in the infra phase. This
module is the verifiable application artifact (audit finding Axis3-F06).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.config import get_settings
from app.lifecycle.cascade import hard_delete_cascade
from app.models.audit import ActorType
from app.models.instance import Instance, LucielState


def grace_window_deadline(started_at: datetime, *, grace_days: int | None = None) -> datetime:
    """Return the instant the grace window expires for a Luciel."""
    days = grace_days if grace_days is not None else get_settings().grace_window_days
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=UTC)
    return started_at + timedelta(days=days)


async def expire_grace_window_if_due(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    now: datetime | None = None,
) -> dict[str, int] | None:
    """Fire the hard-delete cascade for ONE tenant iff its grace window has expired.

    Returns the per-class deletion counts when the cascade ran, or None when the
    tenant is not in ``luciel_grace_window`` or the 30-day deadline has not passed.
    Idempotent: a Luciel that is not in the grace window (e.g. already deleted, or
    reactivated) is left untouched. Must be called inside a tenant-scoped session
    for ``admin_id`` so the cascade's tenant queries are correctly scoped.
    """
    now = now or datetime.now(UTC)
    inst = (
        await session.execute(select(Instance).where(Instance.admin_id == admin_id))
    ).scalar_one_or_none()
    if inst is None or inst.state != LucielState.luciel_grace_window:
        return None
    started = inst.grace_window_started_at
    if started is None:
        # In the grace window but unstamped — do NOT delete on a guess; this is a
        # data-integrity anomaly the ops layer should surface, not silently destroy.
        return None
    if now < grace_window_deadline(started):
        return None  # still inside the 30-day recoverable window

    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=inst.id,
        actor_type=ActorType.platform,
        event_type="lifecycle_transition",
        payload={
            "from": "luciel_grace_window",
            "to": "luciel_hard_deleted",
            "trigger": "grace_window_expiry",
            "grace_window_started_at": started.isoformat(),
        },
    )
    counts = await hard_delete_cascade(session, admin_id)
    return counts


async def run_grace_expiry_sweep(
    candidate_admin_ids: list[uuid.UUID],
    *,
    now: datetime | None = None,
) -> dict[str, dict[str, int]]:
    """One-shot driver: expire every due tenant in ``candidate_admin_ids``.

    Each tenant is processed in its OWN tenant-scoped transaction so a failure in
    one does not roll back another and so the cascade's tenant queries are scoped
    correctly. Returns {admin_id: per-class counts} for tenants that were deleted.

    The platform scheduler supplies ``candidate_admin_ids`` via a platform-role
    query (``SELECT admin_id FROM instances WHERE state = 'luciel_grace_window'``);
    that discovery query + the cron cadence are wired in the infra phase. Importing
    here would create a circular dependency on the engine/session module, so the
    driver takes the candidate list as input and keeps this module pure.
    """
    # Local import avoids a module-load cycle (session -> engine) at import time.
    from app.db.session import claim_sweep_lock, tenant_session

    now = now or datetime.now(UTC)
    deleted: dict[str, dict[str, int]] = {}
    for admin_id in candidate_admin_ids:
        try:
            async with tenant_session(admin_id) as s:
                # The cascade emits audit rows and deletes Secrets Manager entries,
                # neither of which is guarded by a uniqueness constraint, so two
                # overlapping runs would each do it (P2-7). A tenant another run holds
                # is skipped, not waited for.
                if not await claim_sweep_lock(s, "grace_expiry", admin_id):
                    continue
                counts = await expire_grace_window_if_due(s, admin_id=admin_id, now=now)
        except Exception:  # noqa: BLE001 — one tenant never ends the sweep (F036)
            logging.getLogger(__name__).error(
                "sweep_tenant_failed kind=grace_expiry admin_id=%s", admin_id, exc_info=True
            )
            continue
        if counts is not None:
            deleted[str(admin_id)] = counts
    return deleted
