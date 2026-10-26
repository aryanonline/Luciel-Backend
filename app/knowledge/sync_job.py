"""The knowledge sync JOB — a manual sync that outlives the request (§3.2.3).

Live walk run 8 (2026-10-25): "crawl a website" ran inside the owner's request. The
marketing site had grown to 8 pages; they were fetched in one second, then each page
cost one embeddings call (~0.3 s) and one LLM graph-extractor call (4-16 s). The load
balancer's 60 s idle timeout cut the request with a 504 of its own — no CORS headers,
so the browser read it as "Network request failed." — while the sync kept working and
finished with nobody listening. The Architecture doc (§3.2.3) tracked "making the
initial crawl asynchronous" as a follow-up; this module closes it.

The contract:

- ``start_sync_job`` checks the connection exactly as the route always did (404 /
  revoked 409), refuses a second start while a fresh job is ``syncing`` (409), stamps
  ``instance_connections.sync_job = {state: syncing, ...}`` and COMMITS it, then runs
  the sync as an asyncio task in this process and waits for it at most
  ``knowledge_sync_inline_wait_seconds``. A sync that finishes inside the wait answers
  exactly as before (200 ``SyncResultOut``, or the same 409s); one that does not
  answers 202 and the task carries on.
- ``_run_sync_job`` runs the sync in its OWN tenant transaction and stamps the
  terminal state (``synced`` / ``failed`` / ``shrink_pending``) in a SEPARATE short
  transaction (``stamp_job``), so the evidence survives when the sync's transaction
  unwound.
- ``effective_job`` is the read-time stale rule: a ``syncing`` older than
  ``knowledge_sync_job_stale_seconds`` reads as ``failed`` (the process that ran it is
  gone). The row is not rewritten on read.
- The scheduled sync (``app.knowledge.sweep``) stamps the same record with trigger
  ``scheduled`` and skips a connection whose job is fresh and ``syncing``.
- ``drain_sync_jobs`` is awaited from the API process's shutdown (bounded); a job
  still running when the bound ends is cancelled and stamped "interrupted".

Nothing here is owner content: the log line and the metric carry counts, durations and
outcome words only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.connections.secrets import SecretStore, get_secret_store
from app.db.session import tenant_session
from app.knowledge import sync
from app.knowledge.embeddings import EmbeddingError
from app.knowledge.providers import CrawlUnreachable, ProviderUnavailable, provider_for_connection
from app.models.connection import InstanceConnection
from app.models.instance import Instance
from app.observability.metrics import emit_metric

logger = logging.getLogger(__name__)

# --- the vocabulary (the wire's SyncJobOut.state / .trigger) ---------------------- #
SYNCING = "syncing"
SYNCED = "synced"
FAILED = "failed"
SHRINK_PENDING = "shrink_pending"

TRIGGER_MANUAL = "manual"
TRIGGER_SCHEDULED = "scheduled"

# --- owner-facing messages ---------------------------------------------------------- #
ALREADY_RUNNING = "A sync of this source is already running — it will finish on its own."
FAILED_GENERIC = "The sync failed before it finished — nothing was changed; try again."
INTERRUPTED_BY_RESTART = "The sync was interrupted by a restart — try again."
INTERRUPTED_STALE = "The sync was interrupted before it finished — try again."
#: The SAME words the EmbeddingError handler (app.api.errors) has always served.
EMBEDDING_DOWN = (
    "Your knowledge couldn't be indexed right now — the embedding provider "
    "isn't answering. Nothing was saved; please try again in a few minutes."
)
EMBEDDING_RETRY_AFTER_SECONDS = 300

_DEAD_CREDENTIAL_DETAIL = "knowledge sync could not use the stored credential — reconnect"

#: Row-lock bound for the start check and the separate-transaction stamps: a stamp
#: must never wait forever on a transaction that is itself waiting on the stamp.
_LOCK_TIMEOUT = "5s"
_LOCK_NOT_AVAILABLE = "55P03"

ProviderFactory = Callable[[InstanceConnection, SecretStore], sync.SyncProvider]
#: The job's outcome word (the log line and the KnowledgeSyncJob metric's Outcome).
Outcome = Literal[
    "synced",
    "shrink_pending",
    "unreachable",
    "dead_credential",
    "embedding",
    "error",
    "interrupted",
]


def shrink_message(*, remote: int, existing: int) -> str:
    """The 409 a refused shrink has always answered (F107) — and the job's ``error``."""
    return (
        f"The source now reports {remote} item(s) where {existing} are "
        "synced, so nothing was removed. If you did remove them at the source, "
        "sync again and confirm the shrink."
    )


def unavailable_message(exc: Exception) -> str:
    """The 409 an unreachable site / a dead credential has always answered."""
    return f"Sync unavailable: {exc}"


class SyncJobRefused(Exception):
    """The start was refused before anything ran. ``kind`` maps to the HTTP status:
    ``not_found`` → 404; ``revoked`` / ``running`` → 409."""

    def __init__(self, kind: Literal["not_found", "revoked", "running"], message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class JobOutcome:
    """What ``start_sync_job`` hands the route.

    ``deferred`` — the job is still running; ``job`` is the ``syncing`` stamp. Otherwise
    ``job`` is the terminal record, ``outcome`` names it, and ``result`` is the pass
    (``synced`` only)."""

    job: dict[str, Any]
    deferred: bool = False
    outcome: Outcome | None = None
    result: sync.SyncResult | None = None


#: The running manual jobs of THIS process, by connection. A task is removed when it
#: completes; holding it here is also what keeps it from being garbage-collected.
_RUNNING: dict[uuid.UUID, asyncio.Task[JobOutcome]] = {}


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def syncing_stamp(*, started_at: datetime, trigger: str) -> dict[str, Any]:
    return {
        "state": SYNCING,
        "started_at": _iso(started_at),
        "finished_at": None,
        "result": None,
        "error": None,
        "shrink": None,
        "trigger": trigger,
    }


def terminal_stamp(
    started: dict[str, Any],
    *,
    state: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    shrink: dict[str, int] | None = None,
    finished_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        **started,
        "state": state,
        "finished_at": _iso(finished_at or _now()),
        "result": result,
        "error": error,
        "shrink": shrink,
    }


def effective_job_record(job: object, now: datetime | None = None) -> dict[str, Any] | None:
    """The stale rule on a raw ``sync_job`` value (see ``effective_job``)."""
    if not isinstance(job, dict) or not job.get("state"):
        return None
    record: dict[str, Any] = dict(job)
    if record["state"] != SYNCING:
        return record
    now = now or _now()
    started = _parse(record.get("started_at"))
    stale = timedelta(seconds=get_settings().knowledge_sync_job_stale_seconds)
    if started is None or now - started > stale:
        # The process that ran it is gone (a deploy, a crash): never "still syncing"
        # forever, never a success. Read-time only — the row keeps its evidence.
        record.update(state=FAILED, error=INTERRUPTED_STALE, finished_at=None)
    return record


def effective_job(conn: InstanceConnection, now: datetime | None = None) -> dict[str, Any] | None:
    """The connection's sync job as the owner should read it, or ``None`` when it never
    ran one. A ``syncing`` older than ``knowledge_sync_job_stale_seconds`` reads as
    ``failed`` ("interrupted before it finished"); the row is not rewritten."""
    return effective_job_record(getattr(conn, "sync_job", None), now)


def is_fresh_syncing(job: object, now: datetime | None = None) -> bool:
    record = effective_job_record(job, now)
    return record is not None and record["state"] == SYNCING


def _is_lock_timeout(exc: DBAPIError) -> bool:
    orig = exc.orig
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return code == _LOCK_NOT_AVAILABLE or "lock timeout" in str(orig).lower()


async def stamp_job(
    admin_id: uuid.UUID,
    connection_id: uuid.UUID,
    job: dict[str, Any],
    *,
    session: AsyncSession | None = None,
) -> bool:
    """Write ``sync_job`` on one connection. Best-effort: a failed stamp is logged,
    never raised (the stale rule is the safety net for a "syncing" left behind).

    Without ``session`` the write is its own short tenant transaction (the manual job:
    the evidence must survive the sync's own unwound transaction). With ``session`` it
    goes through that session under a SAVEPOINT (the sweep: its tenant transaction may
    already hold this row's lock — a token refresh writes the row — and a second
    transaction would wait on it forever)."""
    stmt = (
        update(InstanceConnection)
        .where(
            InstanceConnection.admin_id == admin_id,
            InstanceConnection.connection_id == connection_id,
        )
        .values(sync_job=job)
        .execution_options(synchronize_session=False)
    )
    try:
        if session is not None:
            async with session.begin_nested():
                await session.execute(stmt)
        else:
            async with tenant_session(admin_id) as s:
                await s.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
                await s.execute(stmt)
    except Exception:  # noqa: BLE001 — the stamp is evidence, never a second failure
        logger.warning("knowledge_sync_job_stamp_failed state=%s", job.get("state"), exc_info=True)
        return False
    return True


#: The name the design notes use for the shared terminal-stamp helper.
_stamp_job = stamp_job


def _counts(result: dict[str, Any] | None) -> dict[str, int]:
    keys = ("added", "updated", "removed", "refused", "failed", "unreached", "graph_deferred")
    if not result:
        return dict.fromkeys(keys, 0)
    return {k: len(result.get(k) or []) for k in keys}


def record_job_outcome(
    *, outcome: str, trigger: str, started_monotonic: float, result: dict[str, Any] | None
) -> None:
    """One INFO line + one EMF datum per job (counts and outcome words only)."""
    duration_ms = int((time.monotonic() - started_monotonic) * 1000)
    c = _counts(result)
    logger.info(
        "knowledge_sync_job outcome=%s trigger=%s duration_ms=%d added=%d updated=%d "
        "removed=%d refused=%d failed=%d unreached=%d graph_deferred=%d halted=%s",
        outcome,
        trigger,
        duration_ms,
        c["added"],
        c["updated"],
        c["removed"],
        c["refused"],
        c["failed"],
        c["unreached"],
        c["graph_deferred"],
        (result or {}).get("halted") or "-",
    )
    emit_metric("KnowledgeSyncJob", dimensions={"Outcome": outcome}, full_dimensions=True)


# --- bookkeeping the manual routes and the job share --------------------------------- #
async def flag_dead_credential(admin_id: uuid.UUID, connection_id: uuid.UUID) -> None:
    """Bookkeeping for a grant the provider just refused (round 7 WP-4c #7), in its
    OWN transaction.

    The manual routes used to pause the sources inside the request's
    ``tenant_session`` and then raise the 409 — which rolled that transaction back,
    so even the pause was lost — and never touched the connection row, so the
    Configure page kept a green "Connected" chip on a dead credential. The sweep
    does both halves (round 5B item 12: pause + ``mark_connection_broken`` → audit +
    once-per-incident attention email); the routes now do the same, before the 409.
    A row the owner never connected (unconfigured / pending) is paused but NOT
    flipped: "error" would claim a grant it never had.
    """
    from app.connections.health import mark_connection_broken  # noqa: PLC0415

    async with tenant_session(admin_id) as s:
        conn = (
            await s.execute(
                select(InstanceConnection).where(
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.connection_id == connection_id,
                )
            )
        ).scalar_one_or_none()
        if conn is None:
            return
        await sync.pause_sync_on_revocation(s, admin_id=admin_id, connection_id=connection_id)
        if conn.status == "connected":
            await mark_connection_broken(s, conn, detail=_DEAD_CREDENTIAL_DETAIL)


async def flag_unreachable_site(admin_id: uuid.UUID, connection_id: uuid.UUID) -> None:
    """Bookkeeping for a crawl that reached no page (round 7 Phase 2 review,
    knowledge #2), in its OWN transaction (the 409 unwinds the request's).

    A website crawl holds no credential, so this is NOT ``flag_dead_credential``:
    the sources read the honest "error" sync state and nothing else changes — no
    D10 stop clock, no pause notice, the connection row keeps its status."""
    async with tenant_session(admin_id) as s:
        await sync.mark_sync_error(s, admin_id=admin_id, connection_id=connection_id)


async def _best_effort(label: str, coro: Any) -> None:
    try:
        await coro
    except Exception:  # noqa: BLE001 — the job still stamps its outcome
        logger.warning("knowledge_sync_job_bookkeeping_failed step=%s", label, exc_info=True)


# --- the job --------------------------------------------------------------------------- #
async def start_sync_job(
    *,
    admin_id: uuid.UUID,
    connection_id: uuid.UUID,
    allow_shrink: bool,
    actor_user_id: uuid.UUID | None,
    provider_factory: ProviderFactory | None = None,
) -> JobOutcome:
    """Start the manual sync of one knowledge_source connection as a job.

    Raises ``SyncJobRefused`` before anything runs (unknown connection, no Luciel,
    revoked, or a fresh job already ``syncing``). Otherwise returns the job's
    outcome when it finishes within ``knowledge_sync_inline_wait_seconds``, else a
    ``deferred`` outcome carrying the ``syncing`` stamp — the task keeps running."""
    settings = get_settings()
    started_at = _now()
    running = _RUNNING.get(connection_id)
    if running is not None and not running.done():
        raise SyncJobRefused("running", ALREADY_RUNNING)
    try:
        async with tenant_session(admin_id) as s:
            # The row lock serialises two near-simultaneous starts; bounded, so a start
            # never queues behind a long transaction that holds the row (a sync whose
            # token refresh wrote it).
            await s.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
            conn = (
                await s.execute(
                    select(InstanceConnection)
                    .where(
                        InstanceConnection.admin_id == admin_id,
                        InstanceConnection.connection_id == connection_id,
                        InstanceConnection.connection_type == "knowledge_source",
                    )
                    .with_for_update(key_share=True)  # FOR NO KEY UPDATE
                )
            ).scalar_one_or_none()
            if conn is None:
                raise SyncJobRefused("not_found", "Sync connection not found.")
            if conn.status == "revoked":
                raise SyncJobRefused("revoked", "Connection is revoked; reconnect before syncing.")
            instance_id = (
                await s.execute(select(Instance.id).where(Instance.admin_id == admin_id))
            ).scalar_one_or_none()
            if instance_id is None:
                raise SyncJobRefused("not_found", "No Luciel.")
            if is_fresh_syncing(conn.sync_job, started_at):
                raise SyncJobRefused("running", ALREADY_RUNNING)
            stamp = syncing_stamp(started_at=started_at, trigger=TRIGGER_MANUAL)
            conn.sync_job = stamp
    except DBAPIError as exc:
        if _is_lock_timeout(exc):
            raise SyncJobRefused("running", ALREADY_RUNNING) from exc
        raise

    task = asyncio.create_task(
        _run_sync_job(
            admin_id=admin_id,
            connection_id=connection_id,
            allow_shrink=allow_shrink,
            stamp=stamp,
            actor_user_id=actor_user_id,
            provider_factory=provider_factory,
        ),
        name=f"knowledge-sync-job-{connection_id}",
    )
    _RUNNING[connection_id] = task

    def _forget(done: asyncio.Task[JobOutcome]) -> None:
        if _RUNNING.get(connection_id) is done:
            del _RUNNING[connection_id]

    task.add_done_callback(_forget)
    finished, _pending = await asyncio.wait(
        {task}, timeout=settings.knowledge_sync_inline_wait_seconds
    )
    if task in finished:
        return task.result()
    return JobOutcome(job=stamp, deferred=True)


async def _run_sync_job(
    *,
    admin_id: uuid.UUID,
    connection_id: uuid.UUID,
    allow_shrink: bool,
    stamp: dict[str, Any],
    actor_user_id: uuid.UUID | None,
    provider_factory: ProviderFactory | None,
) -> JobOutcome:
    """Run one manual sync to its terminal state. Never raises except on cancellation
    (process shutdown), which is stamped "interrupted" and re-raised.

    ``actor_user_id`` is the owner who pressed Sync; the sync's own audit rows stay
    platform-authored exactly as they were when the sync ran inside the request."""
    del actor_user_id  # carried for the call shape; the audit trail is unchanged
    from app.knowledge.sweep import PROVIDER_TO_ORIGIN  # noqa: PLC0415 — sweep imports us

    t0 = time.monotonic()
    factory = provider_factory or provider_for_connection
    outcome: Outcome
    result: sync.SyncResult | None = None
    try:
        try:
            async with tenant_session(admin_id) as s:
                conn = (
                    await s.execute(
                        select(InstanceConnection).where(
                            InstanceConnection.admin_id == admin_id,
                            InstanceConnection.connection_id == connection_id,
                            InstanceConnection.connection_type == "knowledge_source",
                        )
                    )
                ).scalar_one_or_none()
                instance_id = (
                    await s.execute(select(Instance.id).where(Instance.admin_id == admin_id))
                ).scalar_one_or_none()
                if conn is None or instance_id is None or conn.status == "revoked":
                    # Removed or revoked between the start and the run (milliseconds).
                    raise ProviderUnavailable("the connection is no longer connected")
                # Round 5B item 12: refresh at call time — the manual Re-sync button ran
                # on the raw stored token and failed on an hour-old Google token.
                from app.connections.refresh import ensure_fresh_access_token  # noqa: PLC0415

                await ensure_fresh_access_token(
                    s,
                    admin_id,
                    instance_id,
                    connection_type="knowledge_source",
                    connection_id=conn.connection_id,
                )
                provider = factory(conn, get_secret_store())
                result = await sync.sync_connection(
                    s,
                    admin_id=admin_id,
                    instance_id=instance_id,
                    connection_id=conn.connection_id,
                    origin=PROVIDER_TO_ORIGIN.get(conn.provider, conn.provider),
                    provider=provider,
                    allow_shrink=allow_shrink,
                )
            outcome = "synced"
            job = terminal_stamp(stamp, state=SYNCED, result=result.job_dict())
        except sync.SnapshotShrunk as exc:
            # Nothing was deleted (F107); the refusal wrote its own evidence. The owner
            # confirms an intentional shrink by syncing again with ?confirmShrink=true.
            outcome = "shrink_pending"
            job = terminal_stamp(
                stamp,
                state=SHRINK_PENDING,
                error=shrink_message(remote=exc.remote, existing=exc.existing),
                shrink={"remote": exc.remote, "existing": exc.existing},
            )
        except CrawlUnreachable as exc:
            # The site answered no page: a failed sync, not a dead credential.
            await _best_effort("unreachable", flag_unreachable_site(admin_id, connection_id))
            outcome = "unreachable"
            job = terminal_stamp(stamp, state=FAILED, error=unavailable_message(exc))
        except ProviderUnavailable as exc:
            # No usable credential: the pause + the honest connection state (WP-4c #7).
            await _best_effort("dead_credential", flag_dead_credential(admin_id, connection_id))
            outcome = "dead_credential"
            job = terminal_stamp(stamp, state=FAILED, error=unavailable_message(exc))
        except EmbeddingError as exc:
            # The same page the request-path handler raises (E2E-12).
            emit_metric("KnowledgeRetrievalDegraded", dimensions={"Reason": f"ingest:{exc}"[:120]})
            outcome = "embedding"
            job = terminal_stamp(stamp, state=FAILED, error=EMBEDDING_DOWN)
        except Exception:  # noqa: BLE001 — the job always ends in a stamped state
            logger.exception("knowledge_sync_job_failed")
            outcome = "error"
            job = terminal_stamp(stamp, state=FAILED, error=FAILED_GENERIC)
    except asyncio.CancelledError:
        # Process shutdown (drain_sync_jobs' bound ran out): say so on the row, then
        # let the cancellation through.
        job = terminal_stamp(stamp, state=FAILED, error=INTERRUPTED_BY_RESTART)
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(stamp_job(admin_id, connection_id, job), timeout=5)
        record_job_outcome(
            outcome="interrupted", trigger=TRIGGER_MANUAL, started_monotonic=t0, result=None
        )
        raise
    await stamp_job(admin_id, connection_id, job)
    record_job_outcome(
        outcome=outcome,
        trigger=TRIGGER_MANUAL,
        started_monotonic=t0,
        result=job.get("result"),
    )
    return JobOutcome(job=job, outcome=outcome, result=result if outcome == "synced" else None)


async def drain_sync_jobs(timeout: float = 10.0) -> None:
    """Shutdown: give the running manual jobs ``timeout`` seconds to finish, then cancel
    the rest (each stamps "interrupted by a restart") and wait briefly for the stamps."""
    tasks = [t for t in _RUNNING.values() if not t.done()]
    if not tasks:
        return
    _done, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait(pending, timeout=6)
    logger.info("knowledge_sync_jobs_drained finished=%d interrupted=%d", len(_done), len(pending))
