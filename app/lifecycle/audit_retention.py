"""The 1-year audit-log retention the platform already published (§B5, ruling 6).

Every other data class had an enforcer — transcripts and summaries in
``retention.py``, S3 objects in the bucket lifecycle rules — and the audit log had
none. It was append-only with no ceiling, so the published 1-year retention was a
commitment a tenant could disprove by waiting (P1-15).

Enforcement CANNOT live in this module, and that is the point. ``admin_audit_log`` is
owned by ``luciel_audit_owner`` and guarded by an append-only trigger; the app role
holds SELECT and INSERT and no DELETE at all. The one authorized deletion is
``audit_retention_prune``, a SECURITY DEFINER procedure that role owns
(``scripts/lock_audit_log.sql``), which the trigger exempts on identity — and the
trigger independently re-checks the 365-day age of every row, so the boundary is
enforced by the same object that enforces immutability rather than by a function body
someone could later replace.

What is left here is the caller, the audit trail of the pruning itself, and a
description of the lock this sweep depends on.

That last part earns its place here because the dependency is invisible until it
fails. ``lock_audit_log.sql`` cannot be an alembic migration: its objects must be
OWNED by ``luciel_audit_owner`` (the ownership *is* the lock — it is what the
append-only trigger checks when it exempts the one authorized deletion), while
alembic runs as ``luciel_app``, which is deliberately not a member of that role. So a
database can sit at ``alembic upgrade head`` with a schema that looks complete and
still have no ``audit_retention_prune`` — and the only caller is a nightly sweep, so
the omission surfaces as ``undefined_function`` at 3am with the published retention
silently unenforced in the meantime. ``AUDIT_LOCK_CHECKS`` turns that into a loud
failure at the two moments it is still cheap to fix: the migration task
(``scripts/verify_audit_lock.py``, run as the last step of ``scripts/migrate.sh``)
and the sweep entrypoint (``app/jobs/__main__.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.models.audit import ActorType

AUDIT_RETENTION_DAYS = 365

AUDIT_LOCK_REMEDY = (
    "Run the post-migrate superuser step:\n"
    "    python3 scripts/run_sql.py --dsn <master-dsn> --file scripts/lock_audit_log.sql\n"
    "(locally: psql -U postgres -d <db> -f scripts/lock_audit_log.sql). It is idempotent."
)


@dataclass(frozen=True)
class LockCheck:
    """One boolean property of a correctly locked database."""

    name: str
    sql: str
    why: str


# The two helpers below interpolate rather than bind because every argument is a
# module-level literal a few lines further down — there is no runtime input anywhere on
# this path — and because the same strings are handed to psycopg by the migration-task
# CLI, which has no SQLAlchemy to bind with.
def _owned_by_audit_owner(relname: str) -> str:
    return (
        "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "  # noqa: S608  # nosec B608
        f"WHERE c.relname = '{relname}' AND r.rolname = 'luciel_audit_owner')"
    )


def _trigger_exists(relname: str, tgname: str) -> str:
    return (
        "SELECT EXISTS (SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "  # noqa: S608  # nosec B608
        f"WHERE c.relname = '{relname}' AND t.tgname = '{tgname}' AND NOT t.tgisinternal)"
    )


AUDIT_LOCK_CHECKS: tuple[LockCheck, ...] = (
    LockCheck(
        "audit_retention_prune exists, SECURITY DEFINER, owned by luciel_audit_owner",
        "SELECT EXISTS (SELECT 1 FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner "
        "WHERE p.proname = 'audit_retention_prune' AND p.prosecdef "
        "AND r.rolname = 'luciel_audit_owner')",
        "this sweep calls it; without it the 1-year retention is unenforced",
    ),
    LockCheck(
        "luciel_app may EXECUTE audit_retention_prune",
        "SELECT has_function_privilege('luciel_app', "
        "'audit_retention_prune(uuid, int)', 'EXECUTE')",
        "the sweep runs as the app role, not as master",
    ),
    LockCheck(
        "admin_audit_log is owned by luciel_audit_owner",
        _owned_by_audit_owner("admin_audit_log"),
        "ownership is what removes TRUNCATE and DDL from the app role; a REVOKE is not a lock",
    ),
    LockCheck(
        "internal_access_log is owned by luciel_audit_owner",
        _owned_by_audit_owner("internal_access_log"),
        "the staff-access wall (§5.11) is held to the same standard as the audit log",
    ),
    LockCheck(
        "admin_audit_log carries the row-level append-only guard",
        _trigger_exists("admin_audit_log", "trg_audit_append_only"),
        "the trigger, not the procedure body, is what bounds the deletion to aged rows",
    ),
    LockCheck(
        "admin_audit_log carries the statement-level TRUNCATE guard",
        _trigger_exists("admin_audit_log", "trg_audit_no_truncate"),
        "a row-level trigger never fires on TRUNCATE",
    ),
    LockCheck(
        "luciel_app holds no UPDATE/DELETE/TRUNCATE on admin_audit_log",
        "SELECT NOT (has_table_privilege('luciel_app', 'admin_audit_log', 'UPDATE') "
        "OR has_table_privilege('luciel_app', 'admin_audit_log', 'DELETE') "
        "OR has_table_privilege('luciel_app', 'admin_audit_log', 'TRUNCATE'))",
        "append-only means the app role can append and read, and nothing else",
    ),
)


def lock_failures(results: Iterable[tuple[str, bool]]) -> list[str]:
    """Return the names of the checks that did not hold."""
    return [name for name, ok in results if not ok]


async def audit_lock_results(session: AsyncSession) -> list[tuple[str, bool]]:
    """Evaluate every lock check against ``session``'s database.

    Catalog reads only, so any role can run it — the sweep preflights with the app
    role, the migration task with master.
    """
    results = []
    for check in AUDIT_LOCK_CHECKS:
        row = (await session.execute(text(check.sql))).first()
        results.append((check.name, bool(row and row[0])))
    return results


async def prune_audit_log_if_due(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    now: datetime | None = None,
) -> int:
    """Delete this tenant's audit rows past 365 days; return how many went (§B5).

    ``now`` is accepted for the sweep protocol and deliberately NOT used as the cutoff.
    The cutoff is derived server-side from ``now()`` inside the procedure precisely so
    a caller cannot move it: a sweep invoked with a doctored clock must not be able to
    erase last week's audit trail. Tests age their rows, not the clock.

    Emits ``data_retention_hard_delete`` only when something was actually deleted.
    ``retention.py`` emits a zero-count event per class to prove the sweep ran, which
    is the right call for a sweep with a fixed nightly cadence over a handful of
    classes. Here the sweep visits EVERY tenant, so the same choice would append a row
    per tenant per run to the very table being pruned — the retention job would become
    the largest writer to the log it exists to bound (ruling 6).
    """
    deleted = int(
        (
            await session.execute(
                text("SELECT audit_retention_prune(:aid, :days)"),
                {"aid": str(admin_id), "days": AUDIT_RETENTION_DAYS},
            )
        ).scalar_one()
    )
    if deleted:
        await audit.emit(
            session,
            admin_id=admin_id,
            actor_type=ActorType.platform,
            event_type="data_retention_hard_delete",
            payload={
                "data_class": "audit_log",
                "count": deleted,
                "retention_policy": f"{AUDIT_RETENTION_DAYS}d",
            },
        )
    return deleted
