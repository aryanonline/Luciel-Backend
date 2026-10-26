"""CLI dispatcher for the scheduled sweeps: ``python -m app.jobs <sweep>``.

This is the command EventBridge Scheduler overrides the sweep task's container
command with (``infra/lib/compute-stack.ts``), so the schedule name and the sweep
name are the same string in both halves. The result is printed as one line so the
CloudWatch log for a run says what it did, and the exit code is non-zero on failure
so a failed sweep is visible to the schedule's retry policy rather than silent.

A sweep can raise an escalation (the session-close "a booking stalled" page, a
reply the mailbox poll could not deliver). The owner notification for it is a
background task that starts after the tenant's commit; this process is the only
thing that can let it finish, so the sweep is not done until those tasks are
(``_run_sweep``). The API process does the same at lifespan shutdown.

The whole process runs under ONE event loop (``_main``): the audit-lock preflight, the
sweep, the notification drain and the engine dispose. The database engine is a
module-level singleton whose asyncpg pool belongs to the loop that opened it; the
audit sweep used to preflight under one ``asyncio.run`` and sweep under a second, so
its first query pre-pinged a connection from the closed first loop and died with
``got Future ... attached to a different loop`` (every Sunday, 2026-09-27 to 10-25).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

from app.jobs.sweeps import AUDIT_RETENTION, SWEEPS

_LOG = logging.getLogger("app.jobs")

# How long a finished sweep waits for the owner notifications it scheduled. A page is
# roughly ten queries and one provider send; the bound exists so a hung provider call
# cannot hold the Fargate task open, not because a healthy page comes near it.
NOTIFICATION_DRAIN_SECONDS = 60.0


async def _run_sweep(name: str) -> object:
    """Run one sweep, then wait for the owner notifications it scheduled.

    A sweep that writes an escalation row only QUEUES its notification: the task is
    created by the after-commit hook and nothing in the sweep awaits it. Under a bare
    ``asyncio.run(sweep())`` the loop closed as soon as the sweep returned and
    cancelled the task before it opened its database session, so the row stayed
    ``pending`` and nobody was told (the hourly session_finalize close, and the
    mailbox_poll backstop, on the last tenant of every run).

    The drain is in a ``finally`` so the pages for tenants that already committed
    still go out when a later step of the sweep raises. It waits for notifications
    only, not the in-process retry ladder: a refused send has already enqueued its
    durable deferred action and stamped the row ``queued`` before the ladder starts,
    so the deferred sweep owns that retry and this task need not sleep through it.

    Running out of time is logged at ERROR (the level the sweep log group's
    SweepErrors metric filter alarms on) and does not change the exit code: a
    scheduler retry cannot re-page, because the session is already sealed.
    """
    from app.notifications.escalation_dispatch import drain_background_notifications

    try:
        return await SWEEPS[name]()
    finally:
        try:
            await asyncio.wait_for(
                drain_background_notifications(), timeout=NOTIFICATION_DRAIN_SECONDS
            )
        except TimeoutError:
            _LOG.error(
                "sweep_notification_drain_timeout sweep=%s waited_seconds=%s: owner "
                "notifications this sweep scheduled were abandoned unfinished; their "
                "escalation rows may still read pending",
                name,
                NOTIFICATION_DRAIN_SECONDS,
            )


async def _preflight_audit_lock() -> list[str]:
    """Names of the audit-lock checks that do not hold, or [] if the lock is intact.

    The audit sweep's only real work is a call to ``audit_retention_prune``, which
    ``scripts/lock_audit_log.sql`` installs and no migration can (see
    ``app.lifecycle.audit_retention``). Without this, a deploy that ran migrations and
    nothing else fails here with a bare ``undefined_function`` from four frames deep,
    on a nightly schedule nobody is watching.
    """
    from app.db.session import untenanted_session
    from app.lifecycle.audit_retention import audit_lock_results, lock_failures

    async with untenanted_session() as session:
        return lock_failures(await audit_lock_results(session))


class _LockNotApplied(Exception):
    """The audit sweep's preflight found the audit-log lock missing."""

    def __init__(self, failed: list[str]) -> None:
        super().__init__(", ".join(failed))
        self.failed = failed


async def _main(name: str) -> object:
    """Everything the process does with the database, under one event loop.

    The engine is disposed in the same loop before it closes, so no pooled connection
    outlives the loop that owns it (and the process exits with no "Event loop is
    closed" noise from a pool torn down by the garbage collector).
    """
    from app.db.engine import dispose_engine

    try:
        if name == AUDIT_RETENTION and (failed := await _preflight_audit_lock()):
            raise _LockNotApplied(failed)
        return await _run_sweep(name)
    finally:
        await dispose_engine()


def main(argv: list[str] | None = None) -> int:
    # Nothing else configures logging in the sweep container, so without this,
    # ERROR-level lines go through Python's lastResort handler with NO level token
    # — and the sweep log group's `ERROR` metric filter (compute-stack.ts →
    # SweepErrors → the armed alarm) would never match anything. The level token
    # leading the line is what makes an operator-facing logger.error alarmable.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1 or args[0] not in SWEEPS:
        print(f"usage: python -m app.jobs {{{'|'.join(SWEEPS)}}}", file=sys.stderr)
        return 2
    name = args[0]

    try:
        result: object = asyncio.run(_main(name))
    except _LockNotApplied as refused:
        from app.lifecycle.audit_retention import AUDIT_LOCK_REMEDY

        print(
            f"[{name}] refusing to run: the audit-log lock is not applied.\n"
            + "".join(f"  FAIL {check}\n" for check in refused.failed)
            + AUDIT_LOCK_REMEDY,
            file=sys.stderr,
        )
        return 1
    except Exception:  # noqa: BLE001 — reported, then a non-zero exit
        # Logged at ERROR with the sweep's name so the SweepErrors metric filter counts
        # the failed run ITSELF. An uncaught traceback carries no standalone ERROR token:
        # the 2026-09-27..10-25 audit crashes alarmed only because SQLAlchemy's pool
        # happened to log its own ERROR while tearing the dead loop's connection down.
        _LOG.exception("sweep_failed sweep=%s: the run did not complete", name)
        return 1
    print(json.dumps({"sweep": name, "result": result}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
