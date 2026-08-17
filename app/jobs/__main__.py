"""CLI dispatcher for the scheduled sweeps: ``python -m app.jobs <sweep>``.

This is the command EventBridge Scheduler overrides the sweep task's container
command with (``infra/lib/compute-stack.ts``), so the schedule name and the sweep
name are the same string in both halves. The result is printed as one line so the
CloudWatch log for a run says what it did, and the exit code is non-zero on failure
so a failed sweep is visible to the schedule's retry policy rather than silent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys

from app.jobs.sweeps import AUDIT_RETENTION, SWEEPS


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

    if name == AUDIT_RETENTION and (failed := asyncio.run(_preflight_audit_lock())):
        from app.lifecycle.audit_retention import AUDIT_LOCK_REMEDY

        print(
            f"[{name}] refusing to run: the audit-log lock is not applied.\n"
            + "".join(f"  FAIL {check}\n" for check in failed)
            + AUDIT_LOCK_REMEDY,
            file=sys.stderr,
        )
        return 1

    result: object = asyncio.run(SWEEPS[name]())
    print(json.dumps({"sweep": name, "result": result}, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
