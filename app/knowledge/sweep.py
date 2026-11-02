"""Scheduled knowledge sync — the periodic half of "live sync" (§3.2.3).

``sync.sync_connection`` (delta reconcile: add/update/remove) existed but was
reachable only from the two MANUAL dashboard routes, so a "kept in sync" source
was in truth synced exactly as often as the owner remembered to click Re-sync —
and a crawled website was never re-crawled at all. This module is the sweep-side
driver: for one tenant, sync every CONNECTED knowledge connection whose sources
are due under the per-origin cadence.

Cadence is per ORIGIN CLASS, honest about cost (§3.2.3): API-delta providers
(Drive / Notion / CRM-KB) are cheap and run every 6 hours; website crawl is a
full re-fetch + diff — the expensive, feed-less one — and runs every 24 hours.
Manual re-sync from the dashboard remains available for immediacy.

A ``ProviderUnavailable`` (revoked/expired credential) pauses the connection's
sources via the existing §3.2.3 mechanism — the sweep never lets a dead
credential silently serve stale content, and never crashes the tenant loop. A
``CrawlUnreachable`` (a website that answered no page) is NOT a credential: it is an
ordinary failed sync (sources read "error"; no pause, no stop clock, no pause email,
the connection row untouched).
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.db.session import tenant_session
from app.knowledge import service, sync
from app.knowledge import sync_job as sync_jobs
from app.knowledge.embeddings import EMBEDDING_DOWN, EmbeddingError
from app.knowledge.providers import (
    CrawlUnreachable,
    ProviderUnavailable,
    provider_for_connection,
)
from app.models.audit import ActorType
from app.models.connection import InstanceConnection
from app.models.instance import Account, Instance, is_paused
from app.models.knowledge import KnowledgeSource
from app.models.notice_delivery import NoticeDelivery
from app.notifications import email_dispatcher
from app.notifications.email_sender import EmailSender

logger = logging.getLogger(__name__)

#: notice_deliveries ``kind`` values for the D10 notices (round 7 WP-4c #5).
PAUSE_WARNING_KIND = "knowledge_pause_warning"
PAUSE_STOPPED_KIND = "knowledge_pause_stopped"

# Owner-facing names for the knowledge providers the registry does not carry.
_PROVIDER_LABELS: dict[str, str] = {
    "website_crawl": "website crawl",
    "crm_kb": "CRM knowledge base",
}


def _provider_display_name(provider: str) -> str:
    from app.connections import registry  # noqa: PLC0415 — keeps this module import-light

    spec = registry.get_provider(provider)
    if spec is not None:
        return spec.display_name
    return _PROVIDER_LABELS.get(provider, provider)


def pause_notice_id(connection_id: uuid.UUID, paused_since: datetime, kind: str) -> str:
    """The ledger key of one D10 notice: once per (connection, incident, stage)."""
    return f"kbpause:{connection_id}:{paused_since.isoformat()}:{kind}"


async def notify_paused_sources(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    now: datetime | None = None,
    sender: EmailSender | None = None,
) -> int:
    """Owner decision D10 (round 7 WP-4c #5): tell the owner what a revocation costs.

    A connection whose credential was revoked or expired has its sources paused
    (``paused_since`` set once per incident). They keep answering for
    ``service.PAUSED_SERVING_DAYS`` and then retrieval excludes them. The owner is
    emailed ONCE on day 1 ("<sources> will stop answering on <date> unless you
    reconnect") and ONCE when it happens; the notice_deliveries ledger makes "once"
    durable across at-least-once sweep delivery. One email per connection, not per
    source. A sweep that first sees an incident after its stop date sends only the
    stop notice — a warning about a date already past would be a lie. Returns the
    number of notices sent this pass.
    """
    now = now or datetime.now(UTC)
    paused = (
        (
            await session.execute(
                select(KnowledgeSource).where(
                    KnowledgeSource.admin_id == admin_id,
                    KnowledgeSource.soft_deleted_at.is_(None),
                    KnowledgeSource.paused_since.is_not(None),
                    KnowledgeSource.connection_id.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not paused:
        return 0
    email = (
        await session.execute(select(Account.email).where(Account.id == admin_id))
    ).scalar_one_or_none()
    if not email:
        return 0
    by_connection: dict[uuid.UUID, list[KnowledgeSource]] = {}
    stamps: dict[uuid.UUID, datetime] = {}
    for src in paused:
        if src.connection_id is None or src.paused_since is None:
            continue  # filtered above; narrows the types
        by_connection.setdefault(src.connection_id, []).append(src)
        stamp = (
            src.paused_since if src.paused_since.tzinfo else src.paused_since.replace(tzinfo=UTC)
        )
        # One incident per connection: the earliest stamp is the incident's start.
        stamps[src.connection_id] = min(stamps.get(src.connection_id, stamp), stamp)

    sent = 0
    for connection_id, sources in by_connection.items():
        paused_since = stamps[connection_id]
        stops_at = service.stops_serving_at(paused_since)
        kind = PAUSE_STOPPED_KIND if now >= stops_at else PAUSE_WARNING_KIND
        notice_id = pause_notice_id(connection_id, paused_since, kind)
        already = (
            await session.execute(
                select(NoticeDelivery.id).where(
                    NoticeDelivery.admin_id == admin_id, NoticeDelivery.notice_id == notice_id
                )
            )
        ).first()
        if already is not None:
            continue
        conn = (
            await session.execute(
                select(InstanceConnection).where(
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.connection_id == connection_id,
                )
            )
        ).scalar_one_or_none()
        provider_name = _provider_display_name(conn.provider if conn is not None else "")
        names = sorted(s.name for s in sources)
        if kind == PAUSE_WARNING_KIND:
            outcome = await email_dispatcher.send_knowledge_pause_warning(
                session,
                admin_id=admin_id,
                to=email,
                provider_name=provider_name,
                source_names=names,
                stops_at=stops_at,
                sender=sender,
            )
        else:
            outcome = await email_dispatcher.send_knowledge_pause_stopped(
                session,
                admin_id=admin_id,
                to=email,
                provider_name=provider_name,
                source_names=names,
                stopped_at=stops_at,
                grace_days=service.PAUSED_SERVING_DAYS,
                sender=sender,
            )
        if outcome.status == "failed":
            continue  # no ledger row — the next pass retries; the audit row has the failure
        session.add(
            NoticeDelivery(
                admin_id=admin_id,
                notice_id=notice_id,
                kind=kind,
                delivery_status=outcome.status,
                sent_at=now,
            )
        )
        sent += 1
    await session.flush()
    return sent


async def _record_sync_failure(
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    connection_id: uuid.UUID,
    origin: str,
    exc: BaseException,
) -> None:
    """Durable evidence of a failed sync pass, in its own transaction (round 7 WP-4c #3).

    The connection's live sources read ``sync_status = "error"`` and the audit log
    carries the failure CLASS (never provider wording — that stays in the sweep log).
    Written through a fresh ``tenant_session`` so it lands even if the caller's
    transaction is later unwound, exactly as ``sync.sync_connection`` records a
    refused shrink."""
    async with tenant_session(admin_id) as evidence:
        await sync.mark_sync_error(evidence, admin_id=admin_id, connection_id=connection_id)
        await audit.emit(
            evidence,
            admin_id=admin_id,
            instance_id=instance_id,
            actor_type=ActorType.platform,
            event_type="knowledge_source_edited",
            payload={
                "action": "sync_failed",
                "connection_id": str(connection_id),
                "origin": origin,
                "error": type(exc).__name__,
            },
        )


# provider (brand) -> source_origin (§3.2.3 wire vocabulary). Mirrors the map the
# manual routes use (app/api/v1/admin/knowledge.py) — hubspot/salesforce are one
# KB origin, two native connectors.
PROVIDER_TO_ORIGIN: dict[str, str] = {
    "google_drive": "google_drive",
    "notion": "notion",
    "hubspot": "crm_kb",
    "salesforce": "crm_kb",
    "website_crawl": "website_crawl",
}

# §3.2.3 sync cadence: platform-tuned, not admin-configurable.
CRAWL_CADENCE = timedelta(hours=24)
DELTA_CADENCE = timedelta(hours=6)


def _cadence_for(provider: str) -> timedelta:
    return CRAWL_CADENCE if provider == "website_crawl" else DELTA_CADENCE


def _settle_for(
    *, admin_id: uuid.UUID, conn: InstanceConnection, origin: str, now: datetime
) -> sync_jobs.SettleFailure:
    """How a scheduled sync's failure is settled: the same outcomes as the manual job, with
    the sweep's evidence. Runs after the sync's savepoint rolled back and after the removal
    check; the writers use their own short transactions, so no row lock outlives them."""

    async def _failed_sync(exc: Exception) -> None:
        await sync_jobs.best_effort(
            "sync_failed",
            _record_sync_failure(
                admin_id=admin_id,
                instance_id=conn.instance_id,
                connection_id=conn.connection_id,
                origin=origin,
                exc=exc,
            ),
        )
        logger.warning(
            "knowledge_sync_failed provider=%s error=%s", conn.provider, type(exc).__name__
        )

    async def _settle(exc: Exception, stamp: dict[str, Any]) -> sync_jobs.Settled:
        if isinstance(exc, CrawlUnreachable):
            # The site answered no page (maintenance 503, a bot wall's 403, a dead
            # host) — round 7 Phase 2 review, knowledge #2. A crawl holds no
            # credential, so this is NOT the revocation path below: no D10 stop
            # clock, no "your sign-in was revoked" email, no connection flipped to
            # error by the sync (the health sweep has no liveness probe for a crawl
            # and would only flip it back). The sources read the honest "error"
            # state and the next pass retries, exactly like any failed sync.
            await _failed_sync(exc)
            return sync_jobs.ended("unreachable", stamp, error=sync_jobs.unavailable_message(exc))
        if isinstance(exc, ProviderUnavailable):
            # Dead credential: pause + flag (§3.2.3) — never silently serve
            # stale content, never crash the rest of the tenant's connections.
            # Round 5B item 12: the BACKING connection flips through the health
            # transition machinery too (audit + once-per-incident attention
            # email + the dashboard attention counter) — a broken sync used to
            # pause with no notification anywhere. Its own short transaction: the flip's
            # row lock would otherwise block the owner's disconnect behind the next crawl.
            await sync_jobs.best_effort(
                "dead_credential",
                sync_jobs.flag_dead_credential(admin_id, conn.connection_id, now=now),
            )
            logger.info(
                "knowledge_sync_paused provider=%s error=%s", conn.provider, type(exc).__name__
            )
            return sync_jobs.ended(
                "dead_credential", stamp, error=sync_jobs.unavailable_message(exc)
            )
        if isinstance(exc, sync.SnapshotShrunk):
            # The refusal already wrote its own durable evidence (error state + the
            # sync_refused_shrink audit row) before raising; a second failure row
            # here would only muddy the owner's timeline.
            logger.warning(
                "knowledge_sync_refused_shrink provider=%s synced=%d reported=%d",
                conn.provider,
                exc.existing,
                exc.remote,
            )
            return sync_jobs.ended(
                "shrink_pending",
                stamp,
                error=sync_jobs.shrink_message(remote=exc.remote, existing=exc.existing),
                shrink={"remote": exc.remote, "existing": exc.existing},
            )
        # Non-credential failure (quota, parse, provider 500): the sources show the honest
        # "error" sync state instead of a quietly aging "synced" (round 5B item 12); the
        # connection row itself stays as-is (the credential is not implicated). The
        # bookkeeping is the owner's evidence, in its OWN transaction.
        await _failed_sync(exc)
        if isinstance(exc, EmbeddingError):
            return sync_jobs.ended("embedding", stamp, error=EMBEDDING_DOWN)
        return sync_jobs.ended("error", stamp, error=sync_jobs.FAILED_GENERIC)

    return _settle


async def _sync_one_connection(
    *, admin_id: uuid.UUID, conn: InstanceConnection, origin: str, now: datetime
) -> bool:
    """One due connection in its OWN transaction, committed before the next one starts, so
    its data never waits on another connection's work: a removal after that commit is simply
    later than the sync, and a removal before it finds the record gone and unwinds the sync
    (Decision S). Same shape as the manual job. Returns whether the connection synced.

    ``conn`` is the pass's starting list, so only its fixed columns are read from it: the
    status and the job come from a column select, which sees what committed since."""
    job_t0 = time.monotonic()
    connection_id = conn.connection_id
    job_started: dict[str, Any] | None = None
    settled: sync_jobs.Settled
    stamped = False
    try:
        async with tenant_session(admin_id) as s:
            row = (
                await s.execute(
                    select(InstanceConnection.status, InstanceConnection.sync_job).where(
                        InstanceConnection.admin_id == admin_id,
                        InstanceConnection.connection_id == connection_id,
                    )
                )
            ).first()
            if row is None or row.status != "connected":
                # Removed, or marked broken, while this pass worked on earlier connections.
                logger.info("knowledge_sync_skipped_not_connected provider=%s", conn.provider)
                return False
            current_job = row.sync_job
            # Round 7 walk run 8: the owner's manual sync is a job that may still be running
            # in the API process. Leave a live job alone this pass — two concurrent
            # reconciles of one connection would duplicate every new item.
            if await sync_jobs.record_is_live(s, connection_id, current_job):
                logger.info(
                    "knowledge_sync_skipped_job_running provider=%s trigger=%s",
                    conn.provider,
                    (current_job or {}).get("trigger") if isinstance(current_job, dict) else "-",
                )
                return False
            # Outside the savepoint: rolling that back must not release the lock.
            if not await sync.claim_sync_lock(s, connection_id):
                logger.info("knowledge_sync_skipped_job_running provider=%s", conn.provider)
                return False
            # The scheduled pass stamps the same job record (trigger "scheduled"), in its
            # own short transaction, so a manual start and the owner's row see it: only over
            # the record judged dead above, and no stamp means no pass for this connection.
            job_started = sync_jobs.syncing_stamp(
                started_at=datetime.now(UTC), trigger=sync_jobs.TRIGGER_SCHEDULED
            )
            if not await sync_jobs.stamp_syncing_if_unchanged(
                admin_id, connection_id, job_started, seen=current_job
            ):
                logger.warning("knowledge_sync_skipped_stamp_refused provider=%s", conn.provider)
                return False
            # Round 5B item 12: refresh the token at CALL time, in its own short
            # transaction (the manual job's helper): a write's row lock here would make a
            # disconnect wait on the whole sync.
            failure: Exception | None = None
            try:
                await sync_jobs.refresh_token(admin_id, connection_id)
            except Exception as exc:  # noqa: BLE001 — settled and stamped like any failure
                failure = exc
            settled, stamped = await sync_jobs.reconcile_in_transaction(
                s,
                admin_id=admin_id,
                connection_id=connection_id,
                instance_id=conn.instance_id,
                stamp=job_started,
                # Read here, at call time, so the module-level seam stays patchable.
                factory=provider_for_connection,
                settle=_settle_for(admin_id=admin_id, conn=conn, origin=origin, now=now),
                failure=failure,
            )
    except sync.SyncStoppedByOwner:
        assert job_started is not None  # raised only after the stamp
        settled = sync_jobs.stopped(job_started)
    except Exception:  # noqa: BLE001 — one bad source must not stop the sweep
        logger.warning("knowledge_sync_failed provider=%s", conn.provider, exc_info=True)
        if job_started is None:
            return False  # before anything ran; nothing to say on the row
        settled = sync_jobs.ended("error", job_started, error=sync_jobs.FAILED_GENERIC)
        # The transaction died, its commit included, so a stamp written inside it died too.
        stamped = False
    assert job_started is not None  # every path without one has returned
    if settled.outcome == "stopped":
        # The owner removed the source mid-run: its savepoint unwound, nothing kept.
        logger.info("knowledge_sync_stopped_by_owner provider=%s", conn.provider)
    if settled.result is not None and settled.result.refused:
        # The quota refused part of the pass (knowledge #4): the sync itself
        # succeeded and its audit row counts the refusals for the owner.
        logger.warning(
            "knowledge_sync_quota_refused provider=%s refused=%d",
            conn.provider,
            len(settled.result.refused),
        )
    if not stamped:
        await sync_jobs.stamp_with_retry(
            admin_id, connection_id, settled.value, str(job_started["started_at"])
        )
    sync_jobs.record_job_outcome(
        outcome=settled.outcome,
        trigger=sync_jobs.TRIGGER_SCHEDULED,
        started_monotonic=job_t0,
        result=settled.job.get("result"),
    )
    return settled.outcome == "synced"


async def sync_due_connections(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    now: datetime | None = None,
) -> int:
    """Sync every due, CONNECTED knowledge connection for one tenant.

    Due-ness is the newest ``last_synced_at`` across the connection's live
    sources: absent (never synced through the sweep) counts as due. Returns the
    number of connections synced (the sweep log's "did anything happen").

    ``session`` is the tenant's pass: it holds the sweep's lock and runs the maintenance
    passes after the loop, each under its own savepoint. Each connection commits on its
    own (``_sync_one_connection``), so a failure later in the pass cannot take back a sync
    whose stamp already told the owner it landed.
    """
    now = now or datetime.now(UTC)
    # A paused / mid-grace / deleted Luciel is not answering anyone (§3.6.2), so
    # nothing is gained by pulling its sources, and a deleted one must not keep
    # ingesting into rows the cascade is about to purge (round 7 WP-4c #3; the
    # lifecycle work relies on this sweep honouring NON_SERVING_STATES).
    instance = (
        (await session.execute(select(Instance).where(Instance.admin_id == admin_id)))
        .scalars()
        .first()
    )
    if instance is None or is_paused(instance):
        return 0
    connections = (
        (
            await session.execute(
                select(InstanceConnection).where(
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.connection_type == "knowledge_source",
                    InstanceConnection.status == "connected",
                )
            )
        )
        .scalars()
        .all()
    )
    synced = 0
    for conn in connections:
        origin = PROVIDER_TO_ORIGIN.get(conn.provider)
        if origin is None:
            continue  # a provider this sweep does not know how to schedule
        newest = (
            await session.execute(
                select(func.max(KnowledgeSource.last_synced_at)).where(
                    KnowledgeSource.admin_id == admin_id,
                    KnowledgeSource.connection_id == conn.connection_id,
                    KnowledgeSource.soft_deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if newest is not None:
            newest = newest if newest.tzinfo else newest.replace(tzinfo=UTC)
            if now - newest < _cadence_for(conn.provider):
                continue  # fresh enough under the origin's cadence
        if await _sync_one_connection(admin_id=admin_id, conn=conn, origin=origin, now=now):
            synced += 1
    # Each pass below runs under its own SAVEPOINT. They swallow their failures so the
    # sweep goes on, and a database error (a statement timeout, a deadlock) leaves the
    # transaction aborted: without the savepoint, the tenant's COMMIT would come back as a
    # silent ROLLBACK and take the earlier passes' writes, the notice ledger among them.
    #
    # D10: the owner hears about a revoked connection on day 1 (what will stop, when)
    # and on day 7 (that it stopped) — once per incident, never silently.
    try:
        async with session.begin_nested():
            notified = await notify_paused_sources(session, admin_id=admin_id, now=now)
        if notified:
            logger.info("knowledge_pause_notices sent=%d", notified)
    except Exception:  # noqa: BLE001 — never fail the sweep on the notice pass
        logger.exception("knowledge_pause_notices_failed")
    # A changed embedding pin (F121): bring this tenant's older vectors forward in a
    # bounded batch per pass, so retrieval never scores a new-model query against
    # old-model chunks for longer than a few sweeps.
    try:
        async with session.begin_nested():
            reembedded = await service.reembed_outdated_chunks(session, admin_id=admin_id)
        if reembedded:
            logger.info("knowledge_reembed chunks=%d", reembedded)
    except Exception:  # noqa: BLE001 — never fail the sweep on the re-embed pass
        logger.exception("knowledge_reembed_failed")
    # A vector that does not match its own text (F121b): the label check above cannot
    # see it; a bounded batch per pass is checked against a fresh embedding and repaired.
    try:
        async with session.begin_nested():
            checked, repaired = await service.verify_chunk_embeddings(session, admin_id=admin_id)
        if checked:
            logger.info("knowledge_embeddings_verified checked=%d repaired=%d", checked, repaired)
    except Exception:  # noqa: BLE001 — never fail the sweep on the verify pass
        logger.exception("knowledge_embeddings_verify_failed")
    return synced
