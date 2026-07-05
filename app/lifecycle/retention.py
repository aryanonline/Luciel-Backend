"""Age-based data-retention sweep for LIVE tenants (Architecture §3.4.10 / §4.4).

Doctrine (§3.4.10 + §4.4 + Decision #38): retention is ENFORCED by automated
expiration, not left to accumulate. Transcripts and session summaries carry a
1-year retention; past that they are HARD-DELETED. The S3 lifecycle expiration
rule (infra/lib/data-stack.ts) is the object-store backstop; this module is the
Postgres-side reconciliation — the application sweep that deletes aged
``Transcript`` and ``SessionSummary`` rows on LIVE (non-deleted) tenants and
emits a ``data_retention_hard_delete`` audit event per data class.

This is distinct from the account-CLOSE cascade (``app/lifecycle/cascade.py``),
which destroys everything for a tenant being torn down. This sweep runs on
ACTIVE tenants and only removes rows past the age boundary, so an active Luciel's
footprint plateaus rather than growing without bound.

Shape mirrors ``grace_expiry.py``: a per-tenant one-shot ``expire_aged_data`` that
CI/tests call directly, plus a thin multi-tenant driver that iterates candidate
admin_ids. Cross-tenant discovery of which tenants to sweep + the periodic
schedule are the infra/ops half (a platform-role query over ``instances``);
importing the engine/session module here would create a load cycle, so the driver
takes the candidate list as input and keeps this module pure.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.models.audit import ActorType
from app.models.conversation import SessionSummary, Transcript
from app.models.instance import Instance

# §3.4.10 / §4.4 / Decision #38: 1-year retention for transcripts + session
# summaries. Platform policy, not admin-configurable (default; overridable per call
# for tests/ops only).
DEFAULT_RETENTION_DAYS = 365

# (model, data_class label used in the audit payload) for each swept class. Both
# models carry admin_id (TenantMixin) + created_at (TimestampMixin).
_RETAINED_CLASSES: tuple[tuple[type[Transcript] | type[SessionSummary], str], ...] = (
    (Transcript, "transcript"),
    (SessionSummary, "session_summary"),
)


async def expire_aged_data(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    now: datetime | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> dict[str, int]:
    """Hard-delete aged transcripts + summaries for ONE live tenant (§3.4.10).

    Rows whose ``created_at`` is older than ``now - retention_days`` are permanently
    deleted; a ``data_retention_hard_delete`` audit event is emitted per data class
    BEFORE its rows are removed (payload: data_class, count, retention_policy).
    Returns per-class deletion counts. Idempotent — a second run finds nothing aged
    and deletes nothing (and emits a zero-count event, keeping the audit honest).

    Must be called inside a tenant-scoped session for ``admin_id`` so RLS scopes both
    the counts and the deletes to this tenant; the app-level ``admin_id`` filter is
    the belt to RLS's braces. The account-close cascade is unaffected (§3.6.5).
    """
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=retention_days)

    # instance_id is carried on the audit event for dashboard scoping; a tenant has at
    # most one Luciel. None is acceptable (the event is still tenant-scoped by admin_id).
    inst = (
        await session.execute(select(Instance).where(Instance.admin_id == admin_id))
    ).scalar_one_or_none()
    instance_id = inst.id if inst is not None else None

    counts: dict[str, int] = {}
    for model, data_class in _RETAINED_CLASSES:
        aged = int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(model)
                    .where(model.admin_id == admin_id, model.created_at < cutoff)
                )
            ).scalar_one()
        )
        await audit.emit(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            actor_type=ActorType.platform,
            event_type="data_retention_hard_delete",
            payload={
                "data_class": data_class,
                "count": aged,
                "retention_policy": f"age_based_sweep:{retention_days}d",
            },
        )
        result = await session.execute(
            delete(model).where(model.admin_id == admin_id, model.created_at < cutoff)
        )
        counts[data_class] = int(result.rowcount or 0)  # type: ignore[attr-defined]

    await session.flush()
    return counts


async def run_retention_sweep(
    candidate_admin_ids: list[uuid.UUID],
    *,
    now: datetime | None = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> dict[str, dict[str, int]]:
    """One-shot driver: run the age sweep for every tenant in ``candidate_admin_ids``.

    Each tenant is processed in its OWN tenant-scoped transaction so a failure in one
    does not roll back another and so the sweep's queries are correctly RLS-scoped.
    Returns {admin_id: per-class counts} for tenants where anything was deleted.

    The platform scheduler supplies ``candidate_admin_ids`` via a platform-role query
    over ``instances`` (LIVE tenants); that discovery + the cron cadence are wired in
    the infra phase. Importing the session module here would create a load cycle, so
    the driver takes the candidate list as input and keeps this module pure.
    """
    from app.db.session import tenant_session

    now = now or datetime.now(UTC)
    swept: dict[str, dict[str, int]] = {}
    for admin_id in candidate_admin_ids:
        async with tenant_session(admin_id) as s:
            counts = await expire_aged_data(
                s, admin_id=admin_id, now=now, retention_days=retention_days
            )
        if any(counts.values()):
            swept[str(admin_id)] = counts
    return swept
