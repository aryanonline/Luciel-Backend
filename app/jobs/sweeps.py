"""Scheduled sweep entrypoints (Legal §B5 / §A3).

Every sweep's *logic* already existed as a pure, per-tenant, RLS-safe function; what
did not exist was anything that CALLED it, so four compliance commitments never ran.
This module is the missing half: cross-tenant candidate DISCOVERY plus the four
entrypoints EventBridge Scheduler invokes (see ``infra/lib/compute-stack.ts``).

  * ``retention``    — hard-delete transcripts/summaries past 1 year (Legal §B5), then
                       apply each tenant's OPT-IN lead auto-prune rule. Captured Leads
                       are never on the platform's timer — §B5 makes them Admin-managed
                       business data — so leads are pruned only for a tenant that set
                       ``leadRetentionDays`` itself (§3.4.10a); NULL is off and default.
  * ``grace_expiry`` — fire the delete cascade at day 30 of the grace window (§B5).
  * ``close_purge``  — destroy a closed account's shell after its 30-day window (§B5).
  * ``dunning``      — day-7 retrying → reduced when Stripe retries go quiet (§A3).
  * ``usage_reconcile`` — post any billable block Stripe has not been told about yet
                       (§3.4.1b); the backstop for a tenant who crossed a block, hit a
                       Stripe blip, and then stopped conversing.
  * ``unverified_reap`` — hard-purge accounts left ``unverified`` past the 7-day window
                       (§3.7.1a, Legal §A1). The purge is what releases the email
                       address for signup again, so an unscheduled reaper does not just
                       hoard dead credentials — it permanently blocks the address.
  * ``deferred``     — retry queued deferred actions and abandon the ones past their 24h
                       TTL (§3.8.7 rule D). Rule D's promise is that a customer's
                       follow-up is retried when the connection recovers and, failing
                       that, abandoned WITH an owner notification. With no worker calling
                       it, neither half happened: every queued callback sat at attempt 0
                       forever, which is the silent drop the rule forbids.
  * ``audit_retention`` — delete audit rows past the published 1-year retention
                       (§B5, ruling 6). Every other data class had an enforcer; the
                       audit log was append-only with no ceiling, which made the
                       published retention a commitment a tenant could disprove by
                       waiting. The delete itself is a SECURITY DEFINER procedure owned
                       by ``luciel_audit_owner`` — the app role has no DELETE on that
                       table and must not.
  * ``health``       — re-check every non-revoked connection and transition it (§3.8.5).
                       The dormant/expired/error states this produces are what the
                       universal gate reads before it lets a tool run, so an unswept
                       platform gates on a status that was last true the day the admin
                       connected.

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
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import reaper
from app.billing import dunning, metering
from app.connections import deferred_queue, health
from app.db.session import claim_sweep_lock, tenant_session, untenanted_session
from app.lifecycle import audit_retention, grace_expiry, retention, transcript_archive
from app.lifecycle.close_purge import purge_closed_account_if_due
from app.runtime import session_close

RETENTION = "retention"
GRACE_EXPIRY = "grace_expiry"
CLOSE_PURGE = "close_purge"
DUNNING = "dunning"
USAGE_RECONCILE = "usage_reconcile"
UNVERIFIED_REAP = "unverified_reap"
DEFERRED = "deferred"
HEALTH = "health"
AUDIT_RETENTION = "audit_retention"
SESSION_FINALIZE = "session_finalize"
TRANSCRIPT_ARCHIVE = "transcript_archive"
MAILBOX_POLL = "mailbox_poll"

SWEEP_KINDS: tuple[str, ...] = (
    RETENTION,
    GRACE_EXPIRY,
    CLOSE_PURGE,
    DUNNING,
    USAGE_RECONCILE,
    UNVERIFIED_REAP,
    DEFERRED,
    HEALTH,
    AUDIT_RETENTION,
    SESSION_FINALIZE,
    TRANSCRIPT_ARCHIVE,
    MAILBOX_POLL,
)


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
    """Sweep aged transcripts + summaries, and opted-in stale leads, per tenant (§B5)."""
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
    what the job log surfaces as "what this run actually did". A tenant another run
    already holds is skipped rather than waited for (P2-7).
    """
    now = now or datetime.now(UTC)
    acted: list[str] = []
    for admin_id in await sweep_candidates(kind):
        async with tenant_session(admin_id) as s:
            if not await claim_sweep_lock(s, kind, admin_id):
                continue
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


async def _reconcile_usage(session: AsyncSession, *, admin_id: uuid.UUID, now: datetime) -> int:
    return await metering.report_accrued_usage(session, admin_id=admin_id, now=now)


async def run_usage_reconcile(*, now: datetime | None = None) -> list[str]:
    """Post any block Stripe has not been told about yet (§3.4.1b, P1-12).

    The per-turn report is the primary path; this is the backstop for the tenant who
    crossed a block, hit a Stripe blip, and then stopped conversing — there would be no
    next turn to retry on, and the block would be lost.
    """
    return await _per_tenant(USAGE_RECONCILE, _reconcile_usage, now)


async def run_unverified_reap(*, now: datetime | None = None) -> list[str]:
    """Hard-purge accounts left unverified past the 7-day window (§3.7.1a, Legal §A1).

    Discovery deliberately returns EVERY unverified account rather than filtering on
    age in SQL: the window is ``unverified_reap_after_seconds`` in config, and a second
    copy of it inside the SECURITY DEFINER function would silently under-reap the day
    someone shortened it. ``reap_account_if_due`` owns the cutoff; the candidate list is
    a superset and stays small on its own, since anything genuinely aged is deleted on
    the first sweep that sees it.
    """
    return await _per_tenant(UNVERIFIED_REAP, reaper.reap_account_if_due, now)


async def _drain_deferred(
    session: AsyncSession, *, admin_id: uuid.UUID, now: datetime
) -> dict[str, int] | None:
    counts = await deferred_queue.run_sweep(
        session,
        admin_id=admin_id,
        deliverer=deferred_queue.ChannelDeliverer(session),
        now=now,
    )
    # ``_per_tenant`` records a tenant as acted-upon only on a truthy result, and the
    # counts dict is truthy even when every counter is zero.
    return counts if any(counts.values()) else None


async def run_deferred_retry(*, now: datetime | None = None) -> list[str]:
    """Retry due deferred actions; abandon the ones past 24h with notice (§3.8.7 D)."""
    return await _per_tenant(DEFERRED, _drain_deferred, now)


async def _check_connections(
    session: AsyncSession, *, admin_id: uuid.UUID, now: datetime
) -> dict[str, str]:
    from app.channels.tenant_outlook import ProviderDispatchingRefresher

    return await health.run_health_sweep(
        session,
        admin_id=admin_id,
        probe=health.StoredCredentialProbe(),
        # Outlook rows refresh for real (live mode; Microsoft tokens live ~1h);
        # everything else keeps the honest no-refresher → expired path.
        refresher=ProviderDispatchingRefresher(fallback=health.ReconnectOnExpiry()),
        now=now,
    )


async def run_connection_health(*, now: datetime | None = None) -> list[str]:
    """Re-check every tenant's non-revoked connections and transition them (§3.8.5)."""
    return await _per_tenant(HEALTH, _check_connections, now)


async def run_audit_retention(*, now: datetime | None = None) -> list[str]:
    """Delete audit rows past the published 1-year retention (§B5, ruling 6)."""
    return await _per_tenant(AUDIT_RETENTION, audit_retention.prune_audit_log_if_due, now)


async def _finalize_sessions(session: AsyncSession, *, admin_id: uuid.UUID, now: datetime) -> int:
    return await session_close.finalize_due_sessions(session, admin_id=admin_id, now=now)


async def run_session_finalize(*, now: datetime | None = None) -> list[str]:
    """Close sessions past their channel inactivity timeout; summarize (§3.4.7/§3.4.8).

    This is what makes 'summarization fires at the close of every conversation' true:
    a session the channel gave no explicit end signal for (SMS/email/Meta always;
    widget when the tab dies without the beacon) is sealed here, gets its ``ended_at``
    at the honest ``last_message_at + timeout`` boundary, and its §3.4.7 summary is
    written into the cross-session memory store (§3.4.6)."""
    return await _per_tenant(SESSION_FINALIZE, _finalize_sessions, now)


async def _archive_transcripts(session: AsyncSession, *, admin_id: uuid.UUID, now: datetime) -> int:
    return await transcript_archive.archive_aged_transcripts(session, admin_id=admin_id, now=now)


async def run_transcript_archive(*, now: datetime | None = None) -> list[str]:
    """Move ended sessions' transcripts past 90 days to S3 cold storage (#38).

    Runs nightly BEFORE the retention sweep so a session crossing both boundaries in
    one night is archived and then aged out through the same policy path. The hot
    Postgres table plateaus at ~90 days of transcripts; the cold bucket self-bounds
    via the retention sweep's object deletion + the bucket's lifecycle backstop."""
    return await _per_tenant(TRANSCRIPT_ARCHIVE, _archive_transcripts, now)


async def _poll_mailboxes(session: AsyncSession, *, admin_id: uuid.UUID, now: datetime) -> int:
    from app.channels.mailbox_poll import poll_mailbox  # noqa: PLC0415 — lazy: 5-min cadence

    return await poll_mailbox(session, admin_id=admin_id, now=now)


async def run_mailbox_poll(*, now: datetime | None = None) -> list[str]:
    """Pull new mail from every connected BYO Outlook mailbox (§3.1.6a).

    Every 5 minutes — the email channel's inbound transport for mailbox tenants, not
    a compliance sweep. Candidates are only tenants with a CONNECTED outlook row
    (migration 0038), so the run is a no-op scan on a platform with none."""
    return await _per_tenant(MAILBOX_POLL, _poll_mailboxes, now)


SWEEPS: dict[str, Callable[..., Coroutine[object, object, object]]] = {
    RETENTION: run_retention_prune,
    GRACE_EXPIRY: run_grace_purge,
    CLOSE_PURGE: run_close_account_purge,
    DUNNING: run_dunning_transitions,
    USAGE_RECONCILE: run_usage_reconcile,
    UNVERIFIED_REAP: run_unverified_reap,
    DEFERRED: run_deferred_retry,
    HEALTH: run_connection_health,
    AUDIT_RETENTION: run_audit_retention,
    SESSION_FINALIZE: run_session_finalize,
    TRANSCRIPT_ARCHIVE: run_transcript_archive,
    MAILBOX_POLL: run_mailbox_poll,
}
