"""Age-based data-retention sweep for LIVE tenants (Architecture §3.4.10 / §4.4).

Doctrine (§3.4.10 + §4.4 + Decision #38): retention is ENFORCED by automated
expiration, not left to accumulate. Transcripts and session summaries carry a
1-year retention; past that they are HARD-DELETED. The S3 lifecycle expiration
rule (infra/lib/data-stack.ts) is the object-store backstop; this module is the
Postgres-side reconciliation — the application sweep that deletes aged
``Transcript``, ``SessionSummary`` and ``Session`` rows on LIVE (non-deleted)
tenants and emits a ``data_retention_hard_delete`` audit event per data class.

The stored ORIGINALS of knowledge files (app/knowledge/original_store.py) ride
the same sweep as a reconciliation rather than a timer: their bucket has no
expiration by design (an original lives exactly as long as its source), so an
object whose delete failed during a source purge would survive forever. The
sweep lists the tenant's originals prefix and deletes every object whose
knowledge_sources row no longer exists (round 7 WP-8).

Captured Leads are NOT on that platform timer (Legal §B5 makes them Admin-managed
business data). They get a second, opt-in policy in the same sweep —
``prune_stale_leads`` acts only for a tenant that set ``leadRetentionDays`` itself
(§3.4.10a) — so the platform still never timer-deletes leads the Admin did not ask it to.

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

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.knowledge.original_store import (
    ListedOriginal,
    get_original_store,
    original_prefix_for,
)
from app.lifecycle.transcript_store import get_transcript_store
from app.models.audit import ActorType
from app.models.billing import ProcessedStripeEvent
from app.models.conversation import EscalationEvent, Session, SessionSummary, Transcript
from app.models.deferred import DeferredAction
from app.models.instance import Instance
from app.models.knowledge import KnowledgeSource
from app.models.lead import Lead, LeadState
from app.models.trace import RetrievalTraceEvent, ToolTraceEvent

# §3.4.10 / §4.4 / Decision #38: 1-year retention for transcripts + session
# summaries. Platform policy, not admin-configurable (default; overridable per call
# for tests/ops only).
DEFAULT_RETENTION_DAYS = 365
# Terminal deferred-action rows (delivered/abandoned) are receipts, not queue:
# the model's own docstring promised "never accumulates indefinitely", which was
# true of the QUEUE and false of the TABLE until this enforcer.
DEFERRED_TERMINAL_RETENTION_DAYS = 90
# Stripe replay-gate ids: retained past a year with margin (the replay window
# they guard is days), then pruned — the gate's protection does not require
# keeping every event id forever.
STRIPE_EVENT_RETENTION_DAYS = 400
# Marketing contact-form submissions (name + email + free text from anonymous
# visitors): PII with previously NO retention and NO deletion path anywhere.
# Aligned with the 1-year conversation retention (Legal §B5 posture).
CONTACT_SUBMISSION_RETENTION_DAYS = 365
# A stored knowledge original younger than this is never reaped as an orphan: the
# upload route keeps the bytes INSIDE the ingest transaction (keep_original runs
# before the row commits), so a sweep landing in that window would see a key with
# no committed row. An hour is far past any ingest; a real orphan waits one run.
ORIGINAL_ORPHAN_MIN_AGE = timedelta(hours=1)

# (model, data_class label used in the audit payload) for each swept class, children
# before the parent conversation row. Every model here carries admin_id (TenantMixin)
# and created_at (TimestampMixin).
#
# ``Session`` belongs on this list because ``sessions.summary`` is itself conversation
# content — the model's own account of what was said — so deleting the transcripts and
# the session_summaries while leaving the parent row is not a 1-year retention. The
# leftover shells also accumulate without bound on a tenant who never deletes anything,
# which is exactly the plateau this sweep exists to produce.
_RetainedModel = (
    type[Transcript]
    | type[SessionSummary]
    | type[EscalationEvent]
    | type[ToolTraceEvent]
    | type[RetrievalTraceEvent]
    | type[Session]
)

_RETAINED_CLASSES: tuple[tuple[_RetainedModel, str], ...] = (
    (Transcript, "transcript"),
    (SessionSummary, "session_summary"),
    # Conversation-adjacent content that outlived the published 1-year window:
    # escalation events reference sessions/leads and grew forever on a live
    # tenant. Analytics reads them period-scoped only, so the prune cannot
    # distort any served card. Removed before Session (they reference it).
    (EscalationEvent, "escalation_event"),
    # Durable traces (C6, §5.4): same 1-year window as the session content they
    # describe; both reference sessions, so they go before Session too.
    (ToolTraceEvent, "tool_trace_event"),
    (RetrievalTraceEvent, "retrieval_trace_event"),
    (Session, "session"),
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
    BEFORE its rows are removed (payload: data_class, count, retention_policy) —
    and ONLY when the class actually has aged rows. A zero-count run emits nothing:
    the unconditional variant appended ~3 rows per tenant per night to the very
    1-year-retained table this sweep exists to bound — the exact write-amplification
    ``audit_retention.py`` documents refusing (its reasoning, adopted here).
    Returns per-class deletion counts. Idempotent — a second run finds nothing aged,
    deletes nothing, and writes nothing.

    Archived-transcript reconciliation (Commitment #38): an aged ``Session`` may
    carry an ``archive_object_key`` — its transcript moved to S3 cold storage at the
    90-day boundary. The retention sweep is the AUTHORITATIVE deleter of that object
    at the 1-year boundary (the bucket's own lifecycle rule is only the late-safe
    backstop), so the objects are deleted here, best-effort, BEFORE the rows: a store
    outage must not abort the row deletion (the bucket backstop still bounds the
    object), and the failure is recorded on the audit payload rather than raised.

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

    # Cold-store reconciliation first (see docstring): collect the aged sessions'
    # archive keys and delete their S3 objects, best-effort.
    aged_keys = (
        (
            await session.execute(
                select(Session.archive_object_key).where(
                    Session.admin_id == admin_id,
                    Session.created_at < cutoff,
                    Session.archive_object_key.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    objects_deleted = 0
    object_delete_errors = 0
    if aged_keys:
        store = get_transcript_store()
        for key in aged_keys:
            try:
                await store.delete(key=str(key))
                objects_deleted += 1
            except Exception:  # noqa: BLE001 — best-effort; bucket lifecycle is the backstop
                object_delete_errors += 1
        await audit.emit(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            actor_type=ActorType.platform,
            event_type="data_retention_hard_delete",
            payload={
                "data_class": "transcript_archive_object",
                "count": objects_deleted,
                "delete_errors": object_delete_errors,
                "retention_policy": f"age_based_sweep:{retention_days}d",
            },
        )
        counts["transcript_archive_object"] = objects_deleted

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
        if aged == 0:
            counts[data_class] = 0
            continue
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

    # Owner-deleted knowledge sources past their 30-day undo window (audit F109):
    # the delete only tombstoned them so "undo" had something to restore; this is
    # the hard delete the tombstone promised.
    from app.knowledge import service as knowledge_service  # noqa: PLC0415 — import cycle

    purged_sources = await knowledge_service.purge_expired_deletions(
        session, admin_id=admin_id, now=now
    )
    if purged_sources:
        await audit.emit(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            actor_type=ActorType.platform,
            event_type="data_retention_hard_delete",
            payload={
                "data_class": "knowledge_source_grace_expired",
                "count": purged_sources,
                "retention_policy": f"owner_delete_grace:{knowledge_service.DELETE_GRACE_DAYS}d",
            },
        )
    counts["knowledge_source_grace_expired"] = purged_sources

    # Orphaned stored originals (round 7 WP-8): the originals bucket has NO
    # expiration by design — an original lives exactly as long as its source —
    # so an object whose delete failed during a source purge (logged and
    # swallowed there, the prefix sweep at the cascade being the only backstop)
    # would survive an ACTIVE tenant forever. Reconcile the prefix against the
    # rows instead of adding a blanket expiry that would also eat live files.
    counts["knowledge_original_orphan"] = await reap_orphaned_originals(
        session, admin_id=admin_id, instance_id=instance_id, now=now
    )

    # Terminal deferred-action rows: receipts of delivered/abandoned follow-ups,
    # kept 90 days for support forensics, then pruned. The QUEUE (status=queued)
    # is never touched — a pending promise to a customer is not "aged data".
    deferred_cutoff = now - timedelta(days=DEFERRED_TERMINAL_RETENTION_DAYS)
    deferred_result = await session.execute(
        delete(DeferredAction).where(
            DeferredAction.admin_id == admin_id,
            DeferredAction.status.in_(("delivered", "abandoned")),
            DeferredAction.created_at < deferred_cutoff,
        )
    )
    deferred_pruned = int(deferred_result.rowcount or 0)  # type: ignore[attr-defined]
    if deferred_pruned:
        await audit.emit(
            session,
            admin_id=admin_id,
            instance_id=instance_id,
            actor_type=ActorType.platform,
            event_type="data_retention_hard_delete",
            payload={
                "data_class": "deferred_action_terminal",
                "count": deferred_pruned,
                "retention_policy": f"age_based_sweep:{DEFERRED_TERMINAL_RETENTION_DAYS}d",
            },
        )
    counts["deferred_action_terminal"] = deferred_pruned

    # Stripe replay-gate ids past 400 days: the replay window they protect is
    # measured in days; keeping every event id forever was pure growth.
    stripe_cutoff = now - timedelta(days=STRIPE_EVENT_RETENTION_DAYS)
    stripe_result = await session.execute(
        delete(ProcessedStripeEvent).where(
            ProcessedStripeEvent.admin_id == admin_id,
            ProcessedStripeEvent.created_at < stripe_cutoff,
        )
    )
    counts["processed_stripe_event"] = int(stripe_result.rowcount or 0)  # type: ignore[attr-defined]

    await session.flush()
    return counts


def orphaned_original_keys(
    listed: list[ListedOriginal],
    *,
    prefix: str,
    live_source_ids: set[uuid.UUID],
    modified_before: datetime,
) -> list[str]:
    """The keys under a tenant's originals ``prefix`` that no knowledge_sources row
    owns and that were written before ``modified_before``. Key layout is
    ``{prefix}{source_id}/{filename}`` (original_key_for); the source-id segment is
    the ownership link. A key outside the prefix is never a candidate (the store is
    asked for the prefix, but the sweep must not rely on that); a segment that is
    not a UUID names no row and is an orphan too. Tombstoned (soft-deleted) sources
    still have a row, so their originals — the undo window's payload — are kept.
    An object written at or after ``modified_before`` is kept whatever the rows say:
    it may be an upload whose row has not committed yet (ORIGINAL_ORPHAN_MIN_AGE)."""
    orphans: list[str] = []
    for entry in listed:
        key = entry.key
        if not key.startswith(prefix) or entry.last_modified >= modified_before:
            continue
        segment = key[len(prefix) :].split("/", 1)[0]
        try:
            source_id = uuid.UUID(segment)
        except ValueError:
            orphans.append(key)
            continue
        if source_id not in live_source_ids:
            orphans.append(key)
    return orphans


async def reap_orphaned_originals(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID | None = None,
    now: datetime | None = None,
    min_age: timedelta = ORIGINAL_ORPHAN_MIN_AGE,
) -> int:
    """Delete stored originals whose knowledge_sources row is gone (round 7 WP-8).

    Lists the tenant's originals prefix through the bound OriginalStore, keeps
    every object whose source-id segment names an EXISTING row for this tenant
    (soft-deleted rows included — their originals are the undo window's payload)
    and every object younger than ``min_age`` (an upload in flight has its bytes
    before its row commits), and deletes the rest. Emits ONE
    ``data_retention_hard_delete`` audit event with ``data_class``
    ``knowledge_original`` when anything was orphaned (count and delete_errors on
    the payload). Returns the number of objects deleted.

    Best-effort against the store: a listing failure is logged at ERROR (the sweep
    log group's SweepErrors alarm reads that) and reaps nothing this run rather
    than rolling back the tenant's row retention; a per-object delete failure is
    counted on the audit payload and retried by the next nightly run.
    """
    now = now or datetime.now(UTC)
    prefix = original_prefix_for(admin_id)
    store = get_original_store()
    try:
        listed = await store.list_prefix(prefix=prefix)
    except Exception:  # noqa: BLE001 — see docstring
        logging.getLogger(__name__).error(
            "knowledge_original_orphan_list_failed admin_id=%s", admin_id, exc_info=True
        )
        return 0
    if not listed:
        return 0
    live_ids = set(
        (
            await session.execute(
                select(KnowledgeSource.id).where(KnowledgeSource.admin_id == admin_id)
            )
        )
        .scalars()
        .all()
    )
    orphans = orphaned_original_keys(
        listed, prefix=prefix, live_source_ids=live_ids, modified_before=now - min_age
    )
    if not orphans:
        return 0
    deleted = 0
    delete_errors = 0
    for key in orphans:
        try:
            await store.delete(key=key)
            deleted += 1
        except Exception:  # noqa: BLE001 — counted; the next run retries
            delete_errors += 1
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        actor_type=ActorType.platform,
        event_type="data_retention_hard_delete",
        payload={
            "data_class": "knowledge_original",
            "count": deleted,
            "delete_errors": delete_errors,
            "retention_policy": "orphan_reconcile:no_knowledge_sources_row",
        },
    )
    return deleted


async def prune_stale_leads(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    now: datetime | None = None,
) -> int:
    """Apply the tenant's OPT-IN lead auto-prune rule, if it has one (§3.4.10a).

    Captured Leads are the Admin's own business data, so unlike transcripts they are
    never on a platform timer (Legal §B5). This runs only when the Admin has set
    ``instances.lead_retention_days``; a NULL window is the default and prunes nothing.

    Staleness is INACTIVITY, not age: §3.4.10a defines a stale lead as one with "no
    activity (no conversation, no update) for a configurable window", so the cutoff is
    against ``last_activity_at``. Archived leads are exempt — §B5 offers archive as the
    alternative to deletion, so an auto-rule that deleted archived rows anyway would
    make the Admin's choice to archive meaningless.

    Emits one ``lead_pruned`` event per deleted lead carrying only the HASHED lead id,
    matching the manual prune path so erasure-by-key-destruction still holds against the
    immutable log (§3.4.11 / §5.3). Returns the number of leads pruned.
    """
    now = now or datetime.now(UTC)
    inst = (
        await session.execute(select(Instance).where(Instance.admin_id == admin_id))
    ).scalar_one_or_none()
    if inst is None or inst.lead_retention_days is None:
        return 0

    cutoff = now - timedelta(days=inst.lead_retention_days)
    stale = (
        (
            await session.execute(
                select(Lead).where(
                    Lead.admin_id == admin_id,
                    Lead.state != LeadState.archived,
                    Lead.last_activity_at < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    for lead in stale:
        # The same full erasure cascade the dashboard runs (audit F091): a pruned
        # lead's transcripts, summaries, cold-storage objects and follow-ups go too.
        from app.lifecycle.lead_erasure import erase_lead_data  # noqa: PLC0415

        await erase_lead_data(
            session,
            admin_id=admin_id,
            lead=lead,
            event_type="lead_pruned",
            actor_type=ActorType.platform,
            extra_payload={"retention_policy": f"lead_auto_prune:{inst.lead_retention_days}d"},
        )
    await session.flush()
    return len(stale)


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
    from app.db.session import claim_sweep_lock, tenant_session

    now = now or datetime.now(UTC)
    swept: dict[str, dict[str, int]] = {}
    for admin_id in candidate_admin_ids:
        try:
            async with tenant_session(admin_id) as s:
                if not await claim_sweep_lock(s, "retention", admin_id):
                    continue
                counts = await expire_aged_data(
                    s, admin_id=admin_id, now=now, retention_days=retention_days
                )
                # Same transaction as the platform sweep, but a different policy:
                # this one only acts for tenants that opted in (§3.4.10a).
                counts["lead"] = await prune_stale_leads(s, admin_id=admin_id, now=now)
        except Exception:  # noqa: BLE001 — one tenant never ends the sweep (F036)
            logging.getLogger(__name__).error(
                "sweep_tenant_failed kind=retention admin_id=%s", admin_id, exc_info=True
            )
            continue
        if any(counts.values()):
            swept[str(admin_id)] = counts
    return swept


async def expire_platform_data(*, now: datetime | None = None) -> dict[str, int]:
    """Age-enforce the PLATFORM-level tables that have no tenant to iterate.

    Two stores previously had NO deletion path at all:

      * ``contact_submission`` — the marketing site's anonymous form: name,
        email, and up to 5000 chars of free text, NOT tenant-scoped, in no
        cascade and no sweep — unbounded PII retention. Pruned at 1 year,
        aligned with the conversation retention posture (Legal §B5).
      * ``internal_access_log`` — §5.11 keeps a 3-year FLOOR for staff-access
        evidence, but the floor had no ceiling. The prune runs through a
        SECURITY DEFINER function (the app role is deliberately INSERT-only on
        this table) whose SQL clamps the cutoff to now()-3y, so not even a bug
        here can prune younger than the floor.

    No audit emission: ``admin_audit_log`` requires an admin_id and these rows
    belong to no tenant; the deletion counts are returned for the job log.
    """
    from sqlalchemy import text as sa_text  # noqa: PLC0415

    from app.db.session import untenanted_session  # noqa: PLC0415 — see driver note
    from app.models.contact import ContactSubmission  # noqa: PLC0415

    now = now or datetime.now(UTC)
    counts: dict[str, int] = {}
    async with untenanted_session() as s:
        contact_cutoff = now - timedelta(days=CONTACT_SUBMISSION_RETENTION_DAYS)
        result = await s.execute(
            delete(ContactSubmission).where(ContactSubmission.created_at < contact_cutoff)
        )
        counts["contact_submission"] = int(result.rowcount or 0)  # type: ignore[attr-defined]

        pruned = (
            await s.execute(
                sa_text("SELECT internal_access_prune(:cutoff)"),
                {"cutoff": now - timedelta(days=3 * 365)},
            )
        ).scalar_one()
        counts["internal_access_log"] = int(pruned or 0)
    return {k: v for k, v in counts.items() if v}
