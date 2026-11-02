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
  revoked 409), refuses a removed source and a second start while a job is live (409),
  stamps ``instance_connections.sync_job = {state: syncing, ...}`` and COMMITS it, then
  runs the sync as an asyncio task in this process and waits for it at most
  ``knowledge_sync_inline_wait_seconds``. A sync that finishes inside the wait answers
  exactly as before (200 ``SyncResultOut``, or the same 409s); one that does not answers
  202 and the task carries on.
- LIVENESS IS A DATABASE LOCK, NOT AN AGE. ``sync.sync_connection`` takes a
  transaction-scoped advisory lock keyed on the connection, so no two reconciles of one
  connection can overlap whichever caller started them. A ``syncing`` record is live
  while its connection is in ``_RUNNING``, or its lock is held (``pg_locks``), or it is
  younger than ``knowledge_sync_job_start_grace_seconds`` (the gap between the stamp's
  commit and the job taking the lock). Otherwise it READS as failed ("interrupted"); the
  row is not rewritten. A job past any age still reads syncing and refuses a second
  start; an orphan past the grace reads interrupted and admits one at once.
- THE TERMINAL STAMP COMMITS WITH THE OUTCOME. ``_run_sync_job`` refreshes the token in
  its own short transaction, then: outer transaction; lock; SAVEPOINT around the sync;
  on any outcome (a failure rolled back to the savepoint) the stamp in the same outer
  transaction under its own savepoint; commit. Data, stamp and lock release become
  visible together, and the reconcile holds no row lock on the connection, so a
  disconnect never waits on it. The stamp is conditional on its own ``started_at``: a
  cleared or superseded record is never overwritten.
- REMOVING A SOURCE STOPS ITS SYNC. ``sync_connection`` re-reads the status as it goes
  and raises ``SyncStoppedByOwner``; the savepoint unwinds, nothing is kept, outcome
  ``stopped``. A removed row serves no job at all.
- The scheduled sync (``app.knowledge.sweep``) stamps the same record with trigger
  ``scheduled``. Its data commits with the whole tenant's pass, so its lock is held until
  then and its terminal stamps are written right after that commit.
- ``drain_sync_jobs`` runs first in the API process's shutdown (bounded); a job still
  running when the bound ends is cancelled and stamped "interrupted".

Nothing here is owner content: the log line and the metric carry counts, durations and
outcome words only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, or_, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.connections.secrets import SecretStore, get_secret_store
from app.db.session import tenant_session
from app.knowledge import sync
from app.knowledge.embeddings import EMBEDDING_DOWN, EmbeddingError
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
STOPPED_BY_REMOVAL = (
    "This source was removed while it was syncing — nothing from that sync was kept."
)
SOURCE_REMOVED = "This source was removed — connect it again to sync it."

_DEAD_CREDENTIAL_DETAIL = "knowledge sync could not use the stored credential — reconnect"

#: Row-lock bound for the start check and the stamps: neither may queue behind a long
#: transaction that holds the row.
_LOCK_TIMEOUT = "5s"
_LOCK_NOT_AVAILABLE = "55P03"

#: A terminal stamp that failed is retried in short transactions, for this long (seconds),
#: starting at this backoff and doubling. After that the row reads "interrupted".
_STAMP_RETRY_SECONDS = 60.0
_STAMP_RETRY_FIRST_BACKOFF = 0.5

#: How long the shutdown drain lets a running job finish, then how long it waits for the
#: cancelled jobs' stamps. These two plus uvicorn's --timeout-graceful-shutdown and the
#: lifespan's other bounds fit inside the ECS stopTimeout (app/main.py _lifespan).
DRAIN_WAIT_SECONDS = 5.0
DRAIN_STAMP_SECONDS = 6.0

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
    "stopped",
    "already_running",
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
    ``not_found`` → 404; ``revoked`` / ``removed`` / ``running`` → 409."""

    def __init__(
        self, kind: Literal["not_found", "revoked", "removed", "running"], message: str
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class JobOutcome:
    """What ``start_sync_job`` hands the route.

    ``deferred`` — the job is still running; ``job`` is the ``syncing`` stamp. Otherwise
    ``job`` carries the outcome's ``error`` words, ``outcome`` names it, and ``result`` is
    the pass (``synced`` only)."""

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


# --- liveness ------------------------------------------------------------------------ #
def _says_syncing(record: object) -> bool:
    return isinstance(record, dict) and record.get("state") == SYNCING


def job_is_live(
    connection_id: uuid.UUID,
    record: object,
    live_keys: frozenset[int],
    now: datetime | None = None,
) -> bool:
    """Whether a ``syncing`` record has a job behind it: it runs in this process, or its
    lock is held, or it was stamped so recently that its job may not have the lock yet."""
    if not _says_syncing(record):
        return False
    running = _RUNNING.get(connection_id)
    if running is not None and not running.done():
        return True
    if sync.sync_lock_k2(connection_id) in live_keys:
        return True
    assert isinstance(record, dict)  # _says_syncing narrowed it
    started = _parse(record.get("started_at"))
    grace = timedelta(seconds=get_settings().knowledge_sync_job_start_grace_seconds)
    return started is not None and (now or _now()) - started < grace


async def live_keys_for(
    session: AsyncSession, rows: Iterable[InstanceConnection]
) -> frozenset[int]:
    """The live lock keys a read of ``rows`` needs: ``pg_locks`` is read at most once, and
    only when some row actually says syncing."""
    if any(_says_syncing(c.sync_job) and c.status not in sync.REMOVED_STATUSES for c in rows):
        return await sync.live_sync_lock_keys(session)
    return frozenset()


def effective_job(
    conn: InstanceConnection,
    live_keys: frozenset[int] = frozenset(),
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """The connection's sync job as the owner should read it, or ``None`` when it never
    ran one or the owner removed the source. A ``syncing`` with nothing behind it reads
    as ``failed`` ("interrupted before it finished"); the row is not rewritten.

    ``live_keys`` comes from ``live_keys_for`` over the rows being served."""
    raw = getattr(conn, "sync_job", None)
    if not isinstance(raw, dict) or not raw.get("state"):
        return None
    if conn.status in sync.REMOVED_STATUSES:
        return None
    record: dict[str, Any] = dict(raw)
    if record["state"] == SYNCING and not job_is_live(conn.connection_id, record, live_keys, now):
        record.update(state=FAILED, error=INTERRUPTED_STALE, finished_at=None)
    return record


async def record_is_live(session: AsyncSession, connection_id: uuid.UUID, record: object) -> bool:
    """``job_is_live`` for one record read just now (the start check and the sweep's skip)."""
    if not _says_syncing(record):
        return False
    return job_is_live(connection_id, record, await sync.live_sync_lock_keys(session))


async def clear_job_on_reconnect(session: AsyncSession, conn: InstanceConnection) -> None:
    """A connection back to ``connected`` keeps no terminal record — "Connected" beside
    "Last sync failed — reconnect it" tells the owner to do what they just did. A live
    job keeps its record, and will stamp over it."""
    if conn.connection_type != "knowledge_source" or conn.sync_job is None:
        return
    if not await record_is_live(session, conn.connection_id, conn.sync_job):
        conn.sync_job = None


# --- stamps -------------------------------------------------------------------------- #
def _is_lock_timeout(exc: DBAPIError) -> bool:
    orig = exc.orig
    code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return code == _LOCK_NOT_AVAILABLE or "lock timeout" in str(orig).lower()


def _record_started_at() -> Any:
    return func.jsonb_extract_path_text(InstanceConnection.sync_job, "started_at")


async def _conditional_stamp(
    session: AsyncSession,
    admin_id: uuid.UUID,
    connection_id: uuid.UUID,
    value: dict[str, Any] | None,
    own_started_at: str,
) -> bool:
    """Write the job's terminal value (``None`` clears the record) only while the row
    still holds this job's own record. False: it was cleared or superseded, so nothing
    was written — which is right, not a failure."""
    await session.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
    found = await session.execute(
        update(InstanceConnection)
        .where(
            InstanceConnection.admin_id == admin_id,
            InstanceConnection.connection_id == connection_id,
            _record_started_at() == own_started_at,
        )
        .values(sync_job=value)
        .returning(InstanceConnection.connection_id)
        .execution_options(synchronize_session=False)
    )
    return found.first() is not None


async def stamp_with_retry(
    admin_id: uuid.UUID,
    connection_id: uuid.UUID,
    value: dict[str, Any] | None,
    own_started_at: str,
) -> bool:
    """The terminal stamp in its own short transactions, with backoff. Returns whether it
    landed (or had nothing to overwrite); a stamp still failing at the end is an ERROR,
    and the row then reads "interrupted", the truth as far as it can be told."""
    deadline = time.monotonic() + _STAMP_RETRY_SECONDS
    backoff = _STAMP_RETRY_FIRST_BACKOFF
    while True:
        try:
            async with tenant_session(admin_id) as s:
                await _conditional_stamp(s, admin_id, connection_id, value, own_started_at)
            return True
        except Exception:  # noqa: BLE001 — retried below, then logged at ERROR
            if time.monotonic() + backoff > deadline:
                logger.error("knowledge_sync_job_stamp_lost", exc_info=True)
                return False
            logger.warning("knowledge_sync_job_stamp_retry", exc_info=True)
        await asyncio.sleep(backoff)
        backoff *= 2


async def stamp_syncing_if_unchanged(
    admin_id: uuid.UUID,
    connection_id: uuid.UUID,
    stamp: dict[str, Any],
    *,
    seen: object,
) -> bool:
    """The sweep's ``syncing`` stamp, in its own short transaction: UPDATE ... RETURNING,
    only while the row still holds the record the sweep judged dead. False (the record
    moved, or the write failed) means this connection is not the sweep's this pass."""
    seen_started_at = seen.get("started_at") if isinstance(seen, dict) else None
    unchanged = (
        or_(InstanceConnection.sync_job.is_(None), _record_started_at().is_(None))
        if seen_started_at is None
        else _record_started_at() == str(seen_started_at)
    )
    try:
        async with tenant_session(admin_id) as s:
            await s.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_TIMEOUT}'"))
            found = await s.execute(
                update(InstanceConnection)
                .where(
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.connection_id == connection_id,
                    unchanged,
                )
                .values(sync_job=stamp)
                .returning(InstanceConnection.connection_id)
                .execution_options(synchronize_session=False)
            )
            return found.first() is not None
    except Exception:  # noqa: BLE001 — no stamp means no pass, never an unguarded one
        logger.warning("knowledge_sync_job_stamp_failed state=%s", SYNCING, exc_info=True)
        return False


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
    revoked, removed, or a job already live). Otherwise returns the job's outcome when it
    finishes within ``knowledge_sync_inline_wait_seconds``, else a ``deferred`` outcome
    carrying the ``syncing`` stamp — the task keeps running."""
    settings = get_settings()
    started_at = _now()
    running = _RUNNING.get(connection_id)
    if running is not None and not running.done():
        raise SyncJobRefused("running", ALREADY_RUNNING)
    try:
        async with tenant_session(admin_id) as s:
            # The row lock serialises two near-simultaneous starts; bounded, so a start
            # never queues behind a long transaction that holds the row.
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
            if conn.status == "not_connected":
                raise SyncJobRefused("removed", SOURCE_REMOVED)
            instance_id = (
                await s.execute(select(Instance.id).where(Instance.admin_id == admin_id))
            ).scalar_one_or_none()
            if instance_id is None:
                raise SyncJobRefused("not_found", "No Luciel.")
            if await record_is_live(s, connection_id, conn.sync_job):
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


@dataclass(frozen=True)
class _Settled:
    """How a job ended. ``job`` is what the route reads; ``value`` is what the row gets
    (``None`` clears it: a stopped job leaves no record on a removed source)."""

    outcome: Outcome
    job: dict[str, Any]
    value: dict[str, Any] | None
    result: sync.SyncResult | None = None


def _ended(
    outcome: Outcome, stamp: dict[str, Any], *, error: str, shrink: dict[str, int] | None = None
) -> _Settled:
    state = SHRINK_PENDING if shrink is not None else FAILED
    job = terminal_stamp(stamp, state=state, error=error, shrink=shrink)
    return _Settled(outcome=outcome, job=job, value=job)


def _stopped(stamp: dict[str, Any]) -> _Settled:
    job = terminal_stamp(stamp, state=FAILED, error=STOPPED_BY_REMOVAL)
    return _Settled(outcome="stopped", job=job, value=None)


async def _settle_failure(
    exc: Exception, stamp: dict[str, Any], *, admin_id: uuid.UUID, connection_id: uuid.UUID
) -> _Settled:
    """The outcome of a sync that raised, its evidence written.

    Runs AFTER the savepoint rolled back and BEFORE the stamp: at this point the job's
    outer transaction holds no row lock (rolling back to the savepoint released those the
    sync took, and the job never wrote the connection row), so an evidence writer in its
    own transaction can touch the sources and the connection without deadlocking on it."""
    if isinstance(exc, sync.SyncStoppedByOwner):
        return _stopped(stamp)
    if isinstance(exc, sync.SyncAlreadyRunning):
        # Lost a race the start check could not see. Nothing ran; if the winner's stamp
        # already replaced this job's record, the conditional stamp leaves it alone.
        return _ended("already_running", stamp, error=ALREADY_RUNNING)
    if isinstance(exc, sync.SnapshotShrunk):
        # Nothing was deleted (F107); the refusal wrote its own evidence. The owner
        # confirms an intentional shrink by syncing again with ?confirmShrink=true.
        return _ended(
            "shrink_pending",
            stamp,
            error=shrink_message(remote=exc.remote, existing=exc.existing),
            shrink={"remote": exc.remote, "existing": exc.existing},
        )
    if isinstance(exc, CrawlUnreachable):
        # The site answered no page: a failed sync, not a dead credential.
        await _best_effort("unreachable", flag_unreachable_site(admin_id, connection_id))
        return _ended("unreachable", stamp, error=unavailable_message(exc))
    if isinstance(exc, ProviderUnavailable):
        # No usable credential: the pause + the honest connection state (WP-4c #7).
        await _best_effort("dead_credential", flag_dead_credential(admin_id, connection_id))
        return _ended("dead_credential", stamp, error=unavailable_message(exc))
    if isinstance(exc, EmbeddingError):
        # The same page the request-path handler raises (E2E-12).
        emit_metric("KnowledgeRetrievalDegraded", dimensions={"Reason": f"ingest:{exc}"[:120]})
        return _ended("embedding", stamp, error=EMBEDDING_DOWN)
    logger.error("knowledge_sync_job_failed", exc_info=exc)
    return _ended("error", stamp, error=FAILED_GENERIC)


async def _refresh_token(admin_id: uuid.UUID, connection_id: uuid.UUID) -> uuid.UUID:
    """The checks and the token refresh, committed on their own BEFORE the reconcile's
    transaction, so the reconcile holds no row lock on the connection (a disconnect must
    not wait out a crawl). Returns the instance id.

    Round 5B item 12: refresh at call time — the manual Re-sync button ran on the raw
    stored token and failed on an hour-old Google token."""
    from app.connections.refresh import ensure_fresh_access_token  # noqa: PLC0415

    async with tenant_session(admin_id) as s:
        status = (
            await s.execute(
                select(InstanceConnection.status).where(
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.connection_id == connection_id,
                    InstanceConnection.connection_type == "knowledge_source",
                )
            )
        ).scalar_one_or_none()
        instance_id = (
            await s.execute(select(Instance.id).where(Instance.admin_id == admin_id))
        ).scalar_one_or_none()
        if status is None or instance_id is None:
            # Gone between the start and the run (milliseconds).
            raise ProviderUnavailable("the connection is no longer connected")
        if status in sync.REMOVED_STATUSES:
            raise sync.SyncStoppedByOwner(str(connection_id))
        await ensure_fresh_access_token(
            s,
            admin_id,
            instance_id,
            connection_type="knowledge_source",
            connection_id=connection_id,
        )
    return instance_id


async def _reconcile(
    *,
    admin_id: uuid.UUID,
    connection_id: uuid.UUID,
    allow_shrink: bool,
    stamp: dict[str, Any],
    factory: ProviderFactory,
) -> tuple[_Settled, bool]:
    """The outer transaction: lock, SAVEPOINT around the sync, the stamp, commit — so data,
    stamp and lock release become visible together. Returns the outcome and whether the
    stamp already went through that commit (False: the caller retries it)."""
    from app.knowledge.sweep import PROVIDER_TO_ORIGIN  # noqa: PLC0415 — sweep imports us

    own_started_at = str(stamp["started_at"])
    failure: Exception | None = None
    instance_id: uuid.UUID | None = None
    try:
        instance_id = await _refresh_token(admin_id, connection_id)
    except Exception as exc:  # noqa: BLE001 — a job always ends in a stamped state
        failure = exc

    settled: _Settled | None = None
    stamped = False
    try:
        async with tenant_session(admin_id) as s:
            # Before the savepoint: rolling it back must not release the lock (the stamp
            # below still has to commit together with its release).
            if failure is None and not await sync.claim_sync_lock(s, connection_id):
                failure = sync.SyncAlreadyRunning(str(connection_id))
            result: sync.SyncResult | None = None
            if failure is None and instance_id is not None:
                try:
                    async with s.begin_nested():
                        conn = (
                            await s.execute(
                                select(InstanceConnection).where(
                                    InstanceConnection.admin_id == admin_id,
                                    InstanceConnection.connection_id == connection_id,
                                    InstanceConnection.connection_type == "knowledge_source",
                                )
                            )
                        ).scalar_one_or_none()
                        if conn is None:
                            raise ProviderUnavailable("the connection is no longer connected")
                        result = await sync.sync_connection(
                            s,
                            admin_id=admin_id,
                            instance_id=instance_id,
                            connection_id=connection_id,
                            origin=PROVIDER_TO_ORIGIN.get(conn.provider, conn.provider),
                            provider=factory(conn, get_secret_store()),
                            allow_shrink=allow_shrink,
                        )
                except Exception as exc:  # noqa: BLE001 — unwound; the job stamps its outcome
                    failure = exc
                    result = None
            if failure is not None:
                settled = await _settle_failure(
                    failure, stamp, admin_id=admin_id, connection_id=connection_id
                )
            else:
                assert result is not None  # no failure: the sync returned
                job = terminal_stamp(stamp, state=SYNCED, result=result.job_dict())
                settled = _Settled(outcome="synced", job=job, value=job, result=result)
            try:
                async with s.begin_nested():
                    landed = await _conditional_stamp(
                        s, admin_id, connection_id, settled.value, own_started_at
                    )
                    # The record is gone: if a disconnect committed since the sync's last
                    # look, its data must not outlive "no longer crawled" either.
                    if (
                        not landed
                        and settled.outcome == "synced"
                        and await sync.connection_removed(
                            s, admin_id=admin_id, connection_id=connection_id
                        )
                    ):
                        raise sync.SyncStoppedByOwner(str(connection_id))
                stamped = True
            except sync.SyncStoppedByOwner:
                raise
            except Exception:  # noqa: BLE001 — the data still commits; the stamp is retried
                logger.warning("knowledge_sync_job_stamp_failed", exc_info=True)
    except sync.SyncStoppedByOwner:
        return _stopped(stamp), False
    except Exception:  # noqa: BLE001 — the outer transaction itself failed: nothing kept
        logger.exception("knowledge_sync_job_failed")
        return _ended("error", stamp, error=FAILED_GENERIC), False
    assert settled is not None  # the block above assigned it or returned
    return settled, stamped


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
    t0 = time.monotonic()
    own_started_at = str(stamp["started_at"])
    settled: _Settled | None = None
    try:
        settled, stamped = await _reconcile(
            admin_id=admin_id,
            connection_id=connection_id,
            allow_shrink=allow_shrink,
            stamp=stamp,
            factory=provider_factory or provider_for_connection,
        )
        if not stamped:
            await stamp_with_retry(admin_id, connection_id, settled.value, own_started_at)
    except asyncio.CancelledError:
        # Process shutdown (drain_sync_jobs' bound ran out): say so on the row, then let
        # the cancellation through. A job already settled and only retrying its stamp
        # keeps its real outcome.
        value = (
            settled.value
            if settled is not None
            else terminal_stamp(stamp, state=FAILED, error=INTERRUPTED_BY_RESTART)
        )
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(
                _stamp_once(admin_id, connection_id, value, own_started_at), timeout=5
            )
        record_job_outcome(
            outcome=settled.outcome if settled is not None else "interrupted",
            trigger=TRIGGER_MANUAL,
            started_monotonic=t0,
            result=settled.job.get("result") if settled is not None else None,
        )
        raise
    record_job_outcome(
        outcome=settled.outcome,
        trigger=TRIGGER_MANUAL,
        started_monotonic=t0,
        result=settled.job.get("result"),
    )
    return JobOutcome(job=settled.job, outcome=settled.outcome, result=settled.result)


async def _stamp_once(
    admin_id: uuid.UUID,
    connection_id: uuid.UUID,
    value: dict[str, Any] | None,
    own_started_at: str,
) -> None:
    async with tenant_session(admin_id) as s:
        await _conditional_stamp(s, admin_id, connection_id, value, own_started_at)


async def drain_sync_jobs(timeout: float | None = None) -> None:
    """Shutdown: give the running manual jobs ``timeout`` seconds (``DRAIN_WAIT_SECONDS``)
    to finish, then cancel the rest (each stamps "interrupted by a restart") and wait
    briefly for the stamps."""
    tasks = [t for t in _RUNNING.values() if not t.done()]
    if not tasks:
        return
    _done, pending = await asyncio.wait(
        tasks, timeout=DRAIN_WAIT_SECONDS if timeout is None else timeout
    )
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait(pending, timeout=DRAIN_STAMP_SECONDS)
    logger.info("knowledge_sync_jobs_drained finished=%d interrupted=%d", len(_done), len(pending))
