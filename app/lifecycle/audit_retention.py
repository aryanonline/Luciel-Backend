"""The 1-year audit-log retention the platform already published (§B5, ruling 6).

Every other data class had an enforcer — transcripts and summaries in
``retention.py``, S3 objects in the bucket lifecycle rules — and the audit log had
none. It was append-only with no ceiling, so the published 1-year retention was a
commitment a tenant could disprove by waiting (P1-15).

Enforcement CANNOT live in this module, and that is the point. ``admin_audit_log`` is
owned by ``luciel_audit_owner`` and guarded by an append-only trigger; the app role
holds SELECT and INSERT and no DELETE at all. The one authorized deletion is
``audit_retention_prune``, a SECURITY DEFINER procedure that role owns
(``scripts/lock_audit_log.sql``), which the trigger exempts on identity — and the
trigger independently re-checks the 365-day age of every row, so the boundary is
enforced by the same object that enforces immutability rather than by a function body
someone could later replace.

What is left here is the caller and the audit trail of the pruning itself.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.models.audit import ActorType

AUDIT_RETENTION_DAYS = 365


async def prune_audit_log_if_due(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    now: datetime | None = None,
) -> int:
    """Delete this tenant's audit rows past 365 days; return how many went (§B5).

    ``now`` is accepted for the sweep protocol and deliberately NOT used as the cutoff.
    The cutoff is derived server-side from ``now()`` inside the procedure precisely so
    a caller cannot move it: a sweep invoked with a doctored clock must not be able to
    erase last week's audit trail. Tests age their rows, not the clock.

    Emits ``data_retention_hard_delete`` only when something was actually deleted.
    ``retention.py`` emits a zero-count event per class to prove the sweep ran, which
    is the right call for a sweep with a fixed nightly cadence over a handful of
    classes. Here the sweep visits EVERY tenant, so the same choice would append a row
    per tenant per run to the very table being pruned — the retention job would become
    the largest writer to the log it exists to bound (ruling 6).
    """
    deleted = int(
        (
            await session.execute(
                text("SELECT audit_retention_prune(:aid, :days)"),
                {"aid": str(admin_id), "days": AUDIT_RETENTION_DAYS},
            )
        ).scalar_one()
    )
    if deleted:
        await audit.emit(
            session,
            admin_id=admin_id,
            actor_type=ActorType.platform,
            event_type="data_retention_hard_delete",
            payload={
                "data_class": "audit_log",
                "count": deleted,
                "retention_policy": f"{AUDIT_RETENTION_DAYS}d",
            },
        )
    return deleted
