"""Scheduled sweep entrypoints (Legal §B5 / §A3).

Every sweep's *logic* already existed as a pure, per-tenant, RLS-safe function; what
did not exist was anything that CALLED it, so four compliance commitments never ran.
This module is the missing half: cross-tenant candidate DISCOVERY plus the four
entrypoints EventBridge Scheduler invokes (see ``infra/lib/compute-stack.ts``).

  * ``retention``    — hard-delete transcripts/summaries past 1 year (Legal §B5).
                       Captured Leads are deliberately NOT swept: §B5 makes them
                       Admin-managed business data, not timer-deleted.
  * ``grace_expiry`` — fire the delete cascade at day 30 of the grace window (§B5).
  * ``close_purge``  — destroy a closed account's shell after its 30-day window (§B5).
  * ``dunning``      — day-7 retrying → reduced when Stripe retries go quiet (§A3).

Two invariants every sweep holds:

  IDEMPOTENT — schedules deliver at-least-once, and a sweep that ran an hour ago must
  be safe to run again. Each per-tenant function re-derives its own precondition
  (age cutoff, deadline passed, state still ``closed``/``retrying``), so a repeat run
  finds nothing to do rather than double-deleting.

  RLS-SAFE — discovery returns tenant IDS ONLY, via a narrow SECURITY DEFINER
  function (migration 0018). All real work happens inside a per-tenant
  ``tenant_session`` transaction, one transaction per tenant, so a failure on one
  tenant cannot roll back or leak into another.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import text

from app.billing import dunning
from app.db.session import tenant_session, untenanted_session
from app.lifecycle import grace_expiry, retention
from app.lifecycle.close_purge import purge_closed_account_if_due

RETENTION = "retention"
GRACE_EXPIRY = "grace_expiry"
CLOSE_PURGE = "close_purge"
DUNNING = "dunning"

SWEEP_KINDS: tuple[str, ...] = (RETENTION, GRACE_EXPIRY, CLOSE_PURGE, DUNNING)


async def sweep_candidates(kind: str) -> list[uuid.UUID]:
    """Return the tenant ids due for ``kind`` (BYPASSRLS, ids only — migration 0018).

    The app role is NON-BYPASSRLS by design, so a direct cross-tenant SELECT with no
    ``app.admin_id`` set returns zero rows. ``platform_sweep_candidates`` is the
    narrow SECURITY DEFINER seam that answers "which tenants" without exposing any
    tenant DATA. An unknown kind returns an empty list.
    """
    async with untenanted_session() as s:
        rows = await s.execute(
            text("SELECT admin_id FROM platform_sweep_candidates(:kind)"), {"kind": kind}
        )
        return [r[0] for r in rows.all()]


async def run_retention_prune(*, now: datetime | None = None) -> dict[str, dict[str, int]]:
    """Sweep aged transcripts + session summaries on every live tenant (Legal §B5)."""
    return await retention.run_retention_sweep(await sweep_candidates(RETENTION), now=now)


async def run_grace_purge(*, now: datetime | None = None) -> dict[str, dict[str, int]]:
    """Fire the hard-delete cascade for every grace window past day 30 (Legal §B5)."""
    return await grace_expiry.run_grace_expiry_sweep(await sweep_candidates(GRACE_EXPIRY), now=now)


async def _per_tenant(
    kind: str,
    work: Callable[..., Awaitable[object]],
    now: datetime | None,
) -> list[str]:
    """Run ``work`` for each candidate in its OWN tenant-scoped transaction.

    Returns the admin_ids where the work reported a change (truthy result), which is
    what the job log surfaces as "what this run actually did".
    """
    now = now or datetime.now(UTC)
    acted: list[str] = []
    for admin_id in await sweep_candidates(kind):
        async with tenant_session(admin_id) as s:
            result = await work(s, admin_id=admin_id, now=now)
        if result:
            acted.append(str(admin_id))
    return acted


async def run_close_account_purge(*, now: datetime | None = None) -> list[str]:
    """Purge closed-account shells whose 30-day window has expired (Legal §B5)."""
    return await _per_tenant(CLOSE_PURGE, purge_closed_account_if_due, now)


async def run_dunning_transitions(*, now: datetime | None = None) -> list[str]:
    """Apply the day-7 retrying → reduced dunning advance (Legal §A3)."""
    return await _per_tenant(DUNNING, dunning.advance_if_due, now)


SWEEPS: dict[str, Callable[..., Awaitable[object]]] = {
    RETENTION: run_retention_prune,
    GRACE_EXPIRY: run_grace_purge,
    CLOSE_PURGE: run_close_account_purge,
    DUNNING: run_dunning_transitions,
}
