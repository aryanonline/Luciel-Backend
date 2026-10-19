"""Migration 0075 + the lock step: the audit_retention discovery gets its created_at index.

``platform_sweep_candidates('audit_retention')`` filters ``admin_audit_log`` on
``created_at`` alone, and every other index on the table leads with ``admin_id`` — a
weekly full scan of the one table that only grows. ``ix_audit_created_at`` fixes it.

The audit lock decides WHO can build it: ``CREATE INDEX`` needs the table owner, and
after ``scripts/lock_audit_log.sql`` the owner is ``luciel_audit_owner``, which the
app role (alembic's role) is deliberately not a member of. The test DB is provisioned
the way a fresh environment is (alembic, then the lock), so these tests pin all three
paths: the migration built it before the lock; a migration run against the LOCKED
table neither fails nor pretends to own it; and the lock script builds it where it is
missing (every already-locked deployed environment).
"""

from __future__ import annotations

import os
import subprocess

import pytest
from app.db.session import untenanted_session
from sqlalchemy import text

from tests import conftest
from tests.conftest import superuser_exec

pytestmark = [pytest.mark.asyncio]

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REVISION = "0075_audit_created_at_index"
_INDEX = "ix_audit_created_at"
_LOCK_SQL = os.path.join(_REPO_ROOT, "scripts", "lock_audit_log.sql")
_DISCOVERY = (
    "SELECT DISTINCT admin_id FROM admin_audit_log WHERE created_at < now() - interval '365 days'"
)


def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    """Run alembic the way the ops step does (luciel_auth granted only for the run)."""
    superuser_exec("GRANT luciel_auth TO luciel_app")
    try:
        result = subprocess.run(
            ["alembic", *args],
            check=False,
            capture_output=True,
            text=True,
            env=dict(os.environ),
            cwd=_REPO_ROOT,
        )
    finally:
        superuser_exec("REVOKE luciel_auth FROM luciel_app")
    assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stderr[-4000:]}"
    return result


async def _index_def() -> str | None:
    async with untenanted_session() as s:
        return (
            await s.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE tablename = 'admin_audit_log' AND indexname = :n"
                ),
                {"n": _INDEX},
            )
        ).scalar_one_or_none()


def _log_owner() -> str:
    return conftest._admin_psql(
        "SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname = 'admin_audit_log'",
        conftest.TEST_DB,
        tuples_only=True,
    ).strip()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_index_exists_on_created_at_alone_after_migrate_and_lock():
    definition = await _index_def()
    assert definition is not None, "ix_audit_created_at missing after alembic + lock"
    assert definition.endswith("USING btree (created_at)"), definition
    # The lock ran after the migration, so the index went to the audit owner with the table.
    assert _log_owner() == "luciel_audit_owner"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_discovery_predicate_can_range_scan_the_new_index():
    plan = conftest._admin_psql(
        f"SET enable_seqscan = off; EXPLAIN {_DISCOVERY}",
        conftest.TEST_DB,
        tuples_only=True,
    )
    assert _INDEX in plan, plan


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_migration_against_a_locked_log_neither_fails_nor_claims_the_index():
    """Every deployed environment is already locked: as luciel_app the migration cannot
    build or drop an index on a table it does not own. It must leave a notice and step
    aside, never fail the deploy and never report a drop it could not perform."""
    from app.db.engine import dispose_engine

    assert _log_owner() == "luciel_audit_owner"
    await dispose_engine()
    _alembic("downgrade", f"{_REVISION}-1")
    # Not the app role's to drop: the lock step owns it on a locked database.
    assert await _index_def() is not None

    await dispose_engine()
    _alembic("upgrade", "head")
    assert await _index_def() is not None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_lock_step_builds_the_index_where_a_locked_log_lacks_it():
    """The deployed path: the log was locked before 0075 existed, so alembic could not
    build the index and migrate.sh step 3 (scripts/lock_audit_log.sql, master role)
    must. Re-running the lock is idempotent, so it is also safe where it exists."""
    superuser_exec(f"DROP INDEX IF EXISTS {_INDEX}")
    assert await _index_def() is None
    _run_lock_script()
    assert await _index_def() is not None
    # And a second run is a no-op rather than an error.
    _run_lock_script()
    assert await _index_def() is not None
    assert _log_owner() == "luciel_audit_owner"


def _run_lock_script() -> None:
    """Run scripts/lock_audit_log.sql as the superuser exactly the way the test DB's
    provisioning does (tests/conftest.py): the SQL on STDIN with ON_ERROR_STOP. It is
    too large — and too full of ``%`` — to survive a ``-c`` argument through a psql
    shim."""
    with open(_LOCK_SQL, encoding="utf-8") as fh:
        body = fh.read()
    env: dict[str, str] | None
    if conftest._CI_PG:
        argv = [
            conftest._PSQL,
            "-h",
            conftest.PG_HOST,
            "-p",
            conftest.PG_PORT,
            "-U",
            conftest._SUPERUSER,
            "-d",
            conftest.TEST_DB,
            "-v",
            "ON_ERROR_STOP=1",
        ]
        env = dict(os.environ, PGPASSWORD=conftest._SUPERUSER_PW)
    else:
        argv = ["sudo", "-u", "postgres", "psql", "-d", conftest.TEST_DB, "-v", "ON_ERROR_STOP=1"]
        env = None
    done = subprocess.run(  # noqa: S603 — fixed argv, the test's own superuser path
        argv, check=False, capture_output=True, env=env, input=body, text=True, timeout=120
    )
    assert done.returncode == 0, f"lock_audit_log.sql failed:\n{done.stderr[-4000:]}"
