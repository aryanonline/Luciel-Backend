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

import logging
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import reaper
from app.billing import dunning, metering
from app.connections import deferred_queue, health
from app.db.session import claim_sweep_lock, tenant_session, untenanted_session
from app.lifecycle import audit_retention, grace_expiry, retention, transcript_archive
from app.lifecycle.close_purge import purge_closed_account_if_due
from app.models.connection import InstanceConnection
from app.notifications import escalation_dispatch
from app.notifications.backlog import emit_backlog_metric
from app.observability.metrics import emit_metric
from app.runtime import session_close

logger = logging.getLogger(__name__)

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
KNOWLEDGE_SYNC = "knowledge_sync"
BILLING_NOTICES = "billing_notices"
MODEL_CURRENCY = "model_currency"
DAILY_BRIEF = "daily_brief"

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
    KNOWLEDGE_SYNC,
    BILLING_NOTICES,
    MODEL_CURRENCY,
    DAILY_BRIEF,
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
    """Sweep aged transcripts + summaries, and opted-in stale leads, per tenant (§B5).

    Also runs the PLATFORM-level age enforcers (marketing contact-form
    submissions; the internal-access-log's 3-year ceiling) — tables with no
    tenant to iterate, previously with no deletion path at all."""
    results = await retention.run_retention_sweep(await sweep_candidates(RETENTION), now=now)
    platform_counts = await retention.expire_platform_data(now=now)
    if platform_counts:
        results["_platform"] = platform_counts
    return results


async def run_grace_purge(*, now: datetime | None = None) -> dict[str, dict[str, int]]:
    """Fire the hard-delete cascade for every grace window past day 30 (Legal §B5)."""
    return await grace_expiry.run_grace_expiry_sweep(await sweep_candidates(GRACE_EXPIRY), now=now)


async def _per_tenant(
    kind: str,
    work: Callable[..., Awaitable[object]],
    now: datetime | None,
    after_commit: Callable[[uuid.UUID], Awaitable[None]] | None = None,
) -> list[str]:
    """Run ``work`` for each candidate in its OWN tenant-scoped transaction.

    Returns the admin_ids where the work reported a change (truthy result), which is
    what the job log surfaces as "what this run actually did". A tenant another run
    already holds is skipped rather than waited for (P2-7). ``after_commit`` runs once
    the tenant's transaction has COMMITTED; a rolled-back pass never reaches it.
    """
    now = now or datetime.now(UTC)
    acted: list[str] = []
    for admin_id in await sweep_candidates(kind):
        try:
            async with tenant_session(admin_id) as s:
                if not await claim_sweep_lock(s, kind, admin_id):
                    continue
                result = await work(s, admin_id=admin_id, now=now)
        except Exception:  # noqa: BLE001 — one tenant's failure never ends the run (F036)
            # ERROR-level so the SweepErrors metric filter pages an operator; the
            # remaining tenants still get their sweep this run.
            logger.error("sweep_tenant_failed kind=%s admin_id=%s", kind, admin_id, exc_info=True)
            emit_metric("SweepTenantFailure", dimensions={"Sweep": kind})
            continue
        if after_commit is not None:
            await after_commit(admin_id)
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
    # Round 7 delivery hardening: the escalation rows no queue item describes — a
    # ``pending`` row whose notification task died, a ``sent`` row whose delivery
    # receipt never came. Migration 0072 makes their tenant a candidate of this sweep.
    counts.update(
        await escalation_dispatch.sweep_stale_escalations(session, admin_id=admin_id, now=now)
    )
    # ``_per_tenant`` records a tenant as acted-upon only on a truthy result, and the
    # counts dict is truthy even when every counter is zero.
    return counts if any(counts.values()) else None


async def run_deferred_retry(*, now: datetime | None = None) -> list[str]:
    """Retry due deferred actions; abandon the ones past 24h with notice (§3.8.7 D)."""
    acted = await _per_tenant(DEFERRED, _drain_deferred, now)
    # Round 7 WP-4b (item 8): the escalation pages this run left unheard — the datum
    # the ObservabilityStack's EscalationBacklog alarm reads. AFTER the drain, so a
    # page this sweep delivered is not counted against it.
    await emit_backlog_metric()
    return acted


async def _check_connections(
    session: AsyncSession, *, admin_id: uuid.UUID, now: datetime
) -> dict[str, str]:
    from app.connections.probes import DispatchingProbe
    from app.connections.refresh import DispatchingRefresher

    result = await health.run_health_sweep(
        session,
        admin_id=admin_id,
        # Round 6 WP-C: the stored-credential check for every row, then — live
        # only — one identity call at the provider, so a grant revoked at the
        # provider flips within one sweep instead of reading Connected until a
        # customer's booking or lead push fails.
        probe=DispatchingProbe(fallback=health.StoredCredentialProbe()),
        # Every provider with a REFRESH_SPECS entry refreshes for real in live
        # mode (Google/HubSpot/Salesforce/Calendly/Outlook refresh grants,
        # Instagram/Meta long-lived re-exchange); mock and unspecced providers
        # keep the honest no-refresher → expired path. Before this, ONLY
        # Outlook could refresh — Google stored a refresh token nothing ever
        # redeemed and calendars died ~an hour after connect.
        refresher=DispatchingRefresher(fallback=health.ReconnectOnExpiry()),
        now=now,
    )
    await _reapply_sms_webhooks(session, admin_id=admin_id)
    transitions = result
    unhealthy = (
        await session.execute(
            select(func.count())
            .select_from(InstanceConnection)
            .where(
                InstanceConnection.admin_id == admin_id,
                InstanceConnection.status.in_(("error", "expired")),
            )
        )
    ).scalar_one()
    # ConnectionUnhealthy (F032): rows an owner must act on, after this tenant's
    # sweep; CloudWatch sums the per-tenant emissions.
    emit_metric("ConnectionUnhealthy", float(unhealthy))
    return transitions


async def _reapply_sms_webhooks(session: AsyncSession, *, admin_id: uuid.UUID) -> None:
    """Round 5B item 15 — periodic webhook-drift repair, piggybacked on the
    health sweep's cadence. A console edit to the number's Voice/SMS URLs, or a
    platform base-URL change, used to kill inbound with the row still reading
    Connected and NOTHING ever re-checking. Best-effort and self-limiting:
    configure_number_webhooks is a silent no-op without public_api_base_url
    (mock/CI) and never raises; a healthy number gets an idempotent re-point."""
    from app.channels.phone_lifecycle import configure_number_webhooks  # noqa: PLC0415

    rows = (
        (
            await session.execute(
                select(InstanceConnection).where(
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.connection_type == "sms_sender",
                    InstanceConnection.status.in_(("connected", "pending_carrier_registration")),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        destination = (row.non_secret_config or {}).get("destination")
        if isinstance(destination, str) and destination:
            await configure_number_webhooks(
                session,
                admin_id=admin_id,
                instance_id=row.instance_id,
                conn=row,
                e164_number=destination,
                audit_on_success=False,
            )


async def run_connection_health(*, now: datetime | None = None) -> list[str]:
    """Re-check every tenant's non-revoked connections and transition them (§3.8.5)."""
    return await _per_tenant(HEALTH, _check_connections, now)


async def run_audit_retention(*, now: datetime | None = None) -> list[str]:
    """Delete audit rows past the published 1-year retention (§B5, ruling 6)."""
    return await _per_tenant(AUDIT_RETENTION, audit_retention.prune_audit_log_if_due, now)


async def _finalize_sessions(session: AsyncSession, *, admin_id: uuid.UUID, now: datetime) -> int:
    closed = await session_close.finalize_due_sessions(session, admin_id=admin_id, now=now)
    # ActiveSessions (F032): what is still open after the finalize pass, summed
    # across tenants by CloudWatch — the load signal autoscaling and the owner's
    # cost questions both want.
    from app.models.conversation import Session as SessionModel  # noqa: PLC0415

    open_count = (
        await session.execute(
            select(func.count())
            .select_from(SessionModel)
            .where(SessionModel.admin_id == admin_id, SessionModel.ended_at.is_(None))
        )
    ).scalar_one()
    emit_metric("ActiveSessions", float(open_count))
    return closed


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
    """Pull new mail from every serving BYO Outlook mailbox (§3.1.6a).

    Every 5 minutes — the email channel's inbound transport for mailbox tenants, not
    a compliance sweep. Candidates are tenants with a CONNECTED outlook row, or a
    QUIESCING one whose stashed status is connected (migration 0038, widened by 0071
    so a Luciel in its delete drain still has its in-flight thread read — this poll
    is the only inbound door a mailbox has), so the run is a no-op scan on a
    platform with none."""
    return await _per_tenant(MAILBOX_POLL, _poll_mailboxes, now)


async def run_knowledge_sync(*, now: datetime | None = None) -> list[str]:
    """Re-sync due live-sync knowledge connections + re-crawl websites (§3.2.3).

    THE periodic half of "live sync" — the engine (knowledge/sync.sync_connection)
    existed but was reachable only from the two MANUAL dashboard routes, so a
    "kept in sync" source was in truth synced exactly as often as the owner
    remembered to click. Cadence per origin: API-delta providers every 6h,
    website crawl (full re-fetch + diff, the expensive one) every 24h.

    The terminal stamps wait for the tenant's commit: until then the pass's data is
    invisible and its locks are held, so "syncing" is the truth. Written after it, in
    their own transaction, they hold no row lock over the rest of the pass; a rolled-back
    pass writes none, and its rows read interrupted, which is also the truth."""
    from app.knowledge.sweep import (  # noqa: PLC0415
        PendingStamp,
        sync_due_connections,
        write_pending_stamps,
    )

    pending: dict[uuid.UUID, list[PendingStamp]] = {}

    async def _sync_knowledge(session: AsyncSession, *, admin_id: uuid.UUID, now: datetime) -> int:
        return await sync_due_connections(
            session, admin_id=admin_id, now=now, pending_stamps=pending.setdefault(admin_id, [])
        )

    async def _stamp_after_commit(admin_id: uuid.UUID) -> None:
        await write_pending_stamps(admin_id, pending.pop(admin_id, []))

    return await _per_tenant(KNOWLEDGE_SYNC, _sync_knowledge, now, after_commit=_stamp_after_commit)


async def _deliver_billing_notices(
    session: AsyncSession, *, admin_id: uuid.UUID, now: datetime
) -> int:
    from app.billing.notice_delivery import deliver_due_notices  # noqa: PLC0415 — lazy import

    return await deliver_due_notices(session, admin_id=admin_id, now=now)


async def run_billing_notices(*, now: datetime | None = None) -> list[str]:
    """Email every due billing notice, once per tenant per notice (§A5 / §A10).

    THE email half of the account-notice commitments: the §A5 allowance-reduction
    notice ("BOTH by email AND as an in-dashboard notification") and any
    operator-published §A10/§B6 notice, plus the Vision §7 ~80%-through-a-block
    pay-as-you-go heads-up (once per billing period). The dashboard halves have
    always rendered; before this sweep nothing ever sent the emails, so the
    commitment was met only for owners who happened to log in. Idempotency is the
    ``notice_deliveries`` ledger, not the schedule."""
    return await _per_tenant(BILLING_NOTICES, _deliver_billing_notices, now)


async def run_model_currency(*, now: datetime | None = None) -> dict[str, object]:
    """Verify the pinned LLM model IDs still exist at their providers (§3.4.3).

    PLATFORM-level like the retention sweep's ``expire_platform_data`` half: no
    tenant loop, no candidates — model pins belong to the platform, not a tenant.
    A retired CHAT pin is auto-swapped to its same-family successor (platform-
    secret write + service restart + operator email); the embedding pin and any
    unremediable case alert and RAISE, so the nonzero exit + the ERROR log line
    reach the sweep log group's SweepErrors alarm. Mock mode is a no-op."""
    from app.runtime.model_currency import run_model_currency_check  # noqa: PLC0415

    # A real date always rides along: the weekly newer-same-family nudge
    # (Phase 7) fires on its weekday only when the caller stamps one, and the
    # scheduled entrypoint invokes sweeps with no arguments.
    return await run_model_currency_check(now=now or datetime.now(UTC))


async def _send_daily_brief(session: AsyncSession, *, admin_id: uuid.UUID, now: datetime) -> int:
    from app.employee.brief import send_daily_brief_if_due  # noqa: PLC0415 — lazy import

    return await send_daily_brief_if_due(session, admin_id=admin_id, now=now)


async def run_daily_brief(*, now: datetime | None = None) -> list[str]:
    """Email each owner's morning brief once per LOCAL day (round 6 WP-F).

    Hourly, because owners sit in different zones: a tenant is due once its local
    clock passes 07:00 and no ``brief:<date>`` ledger row exists. "What I did
    yesterday, what I need today" — counts and links from rows that already exist,
    never narration. ``daily_brief_enabled`` is the opt-out."""
    return await _per_tenant(DAILY_BRIEF, _send_daily_brief, now)


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
    KNOWLEDGE_SYNC: run_knowledge_sync,
    BILLING_NOTICES: run_billing_notices,
    MODEL_CURRENCY: run_model_currency,
    DAILY_BRIEF: run_daily_brief,
}
