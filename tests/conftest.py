"""Test fixtures: a real Postgres test database with RLS, and a Redis flush.

The isolation suite MUST run against a real Postgres with the app connecting as a
NON-superuser, NON-BYPASSRLS role — otherwise RLS is silently inert and the tests
prove nothing. conftest points the app at ``luciel_test`` (created/migrated here)
and asserts the connection role cannot bypass RLS before any test runs.
"""

from __future__ import annotations

import asyncio
import io
import os
import subprocess
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text

# Point the whole app at the test DB BEFORE app modules read settings.
TEST_DB = os.environ.get("LUCIEL_TEST_DB", "luciel_test")
APP_USER = os.environ.get("LUCIEL_APP_USER", "luciel_app")
APP_PW = os.environ.get("LUCIEL_APP_PW", "luciel_dev_pw")
PG_HOST = os.environ.get("LUCIEL_PG_HOST", "127.0.0.1")
PG_PORT = os.environ.get("LUCIEL_PG_PORT", "5432")

os.environ["LUCIEL_DATABASE_URL"] = (
    f"postgresql+asyncpg://{APP_USER}:{APP_PW}@{PG_HOST}:{PG_PORT}/{TEST_DB}"
)
os.environ["LUCIEL_DATABASE_URL_SYNC"] = (
    f"postgresql+psycopg://{APP_USER}:{APP_PW}@{PG_HOST}:{PG_PORT}/{TEST_DB}"
)
# Tests run over http:// (ASGI transport); the Secure cookie flag would make the
# client withhold the session cookie. Disable Secure in the TEST env only
# (prod keeps Secure=True).
os.environ["LUCIEL_COOKIE_SECURE"] = "false"


# Superuser psql invocation differs between the local sandbox (peer auth via
# `sudo -u postgres`) and CI (a Postgres service container reached over TCP).
_CI_PG = os.environ.get("LUCIEL_SUPERUSER_TCP") == "1" or os.environ.get("CI") == "true"
_SUPERUSER = os.environ.get("LUCIEL_SUPERUSER", "postgres")
_SUPERUSER_PW = os.environ.get("PGPASSWORD", "postgres")
# The psql executable. Overridable for hosts with no native client (e.g. a Windows
# dev box driving the docker-compose db through a `docker exec` shim script) — the
# same environment-portability seam as _CI_PG above. CI/default is plain "psql".
_PSQL = os.environ.get("LUCIEL_PSQL", "psql")


def _admin_psql(sql: str, db: str = "postgres") -> str:
    """Run ``sql`` as the superuser; returns psql's stdout (decoded)."""
    if _CI_PG:
        env = dict(os.environ, PGPASSWORD=_SUPERUSER_PW)
        done = subprocess.run(
            [
                _PSQL,
                "-h",
                PG_HOST,
                "-p",
                PG_PORT,
                "-U",
                _SUPERUSER,
                "-d",
                db,
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                sql,
            ],
            check=True,
            capture_output=True,
            env=env,
        )
    else:
        done = subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql],
            check=True,
            capture_output=True,
        )
    return done.stdout.decode("utf-8", "replace") if done.stdout else ""


def superuser_exec(sql: str) -> None:
    """Run a statement against the TEST DB as a superuser.

    Used by the audit-chain tamper tests: after the B1 immutability lock,
    admin_audit_log can be mutated ONLY by a superuser (by design), so a
    deliberate tamper for a tamper-evidence test must take the superuser path
    rather than the old self-grant loophole.
    """
    if _CI_PG:
        env = dict(os.environ, PGPASSWORD=_SUPERUSER_PW)
        subprocess.run(
            [
                _PSQL,
                "-h",
                PG_HOST,
                "-p",
                PG_PORT,
                "-U",
                _SUPERUSER,
                "-d",
                TEST_DB,
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                sql,
            ],
            check=True,
            capture_output=True,
            env=env,
        )
    else:
        subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-d", TEST_DB, "-v", "ON_ERROR_STOP=1", "-c", sql],
            check=True,
            capture_output=True,
        )


@pytest.fixture(scope="session", autouse=True)
def _provision_test_db() -> None:
    """Create the test DB (owned by the app role), extensions, and run migrations."""
    # Recreate a clean test DB.
    _admin_psql(f"DROP DATABASE IF EXISTS {TEST_DB}")
    _admin_psql(f"CREATE DATABASE {TEST_DB} OWNER {APP_USER}")
    _admin_psql("CREATE EXTENSION IF NOT EXISTS vector", db=TEST_DB)
    _admin_psql('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"', db=TEST_DB)
    # Mirror the infra bootstrap (scripts/bootstrap_roles.sql): the narrow
    # BYPASSRLS auth role + its schema grants, so the migration can own the
    # auth_lookup_account function on the test DB exactly as in dev/prod.
    _admin_psql(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='luciel_auth') "
        "THEN CREATE ROLE luciel_auth NOLOGIN NOSUPERUSER BYPASSRLS; END IF; END $$;"
    )
    _admin_psql(f"GRANT luciel_auth TO {APP_USER}")
    # Append-only audit-log owner role (Architecture §5.3 / audit finding B1): a
    # NOLOGIN role that the app role is NOT a member of, so it cannot re-grant
    # itself UPDATE/DELETE on admin_audit_log nor drop the guard. Mirrors
    # scripts/bootstrap_roles.sql.
    _admin_psql(
        "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='luciel_audit_owner') "
        "THEN CREATE ROLE luciel_audit_owner NOLOGIN NOSUPERUSER NOBYPASSRLS; END IF; END $$;"
    )
    _admin_psql(f"ALTER SCHEMA public OWNER TO {APP_USER}", db=TEST_DB)
    _admin_psql(f"GRANT ALL ON SCHEMA public TO {APP_USER}", db=TEST_DB)
    _admin_psql("GRANT USAGE, CREATE ON SCHEMA public TO luciel_auth", db=TEST_DB)
    _admin_psql("GRANT USAGE ON SCHEMA public TO luciel_audit_owner", db=TEST_DB)

    # Run migrations via alembic against the test DB.
    env = dict(os.environ)
    subprocess.run(
        ["alembic", "upgrade", "head"],
        check=True,
        capture_output=True,
        env=env,
        cwd=os.path.dirname(os.path.dirname(__file__)),
    )

    # Apply the audit-log immutability lock as a superuser, exactly as the
    # post-migrate ops step (scripts/lock_audit_log.sql) does in dev/prod. This is
    # what makes the append-only guarantee a true lock (ownership by
    # luciel_audit_owner + guard trigger) rather than a revocable grant.
    _repo_root = os.path.dirname(os.path.dirname(__file__))
    _lock_sql = os.path.join(_repo_root, "scripts", "lock_audit_log.sql")
    # Pass the SQL on STDIN rather than `-f <path>`: under `sudo -u postgres` the
    # postgres OS user may not be able to read the repo path (Permission denied),
    # so we read the file ourselves and pipe its contents in.
    _lock_body = open(_lock_sql, encoding="utf-8").read()
    if _CI_PG:
        _env = dict(os.environ, PGPASSWORD=_SUPERUSER_PW)
        subprocess.run(
            [
                _PSQL,
                "-h",
                PG_HOST,
                "-p",
                PG_PORT,
                "-U",
                _SUPERUSER,
                "-d",
                TEST_DB,
                "-v",
                "ON_ERROR_STOP=1",
            ],
            check=True,
            capture_output=True,
            env=_env,
            input=_lock_body,
            text=True,
        )
    else:
        subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-d", TEST_DB, "-v", "ON_ERROR_STOP=1"],
            check=True,
            capture_output=True,
            input=_lock_body,
            text=True,
        )


@pytest_asyncio.fixture
async def assert_no_bypass() -> None:
    """Guard: the app connection must not be a superuser / BYPASSRLS role."""
    from app.db.engine import get_engine

    async with get_engine().connect() as conn:
        row = (
            await conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
            )
        ).one()
        assert row.rolsuper is False, "app role must NOT be superuser (RLS would be bypassed)"
        assert row.rolbypassrls is False, "app role must NOT have BYPASSRLS"
        member = (
            await conn.execute(text("SELECT pg_has_role(current_user, 'luciel_auth', 'MEMBER')"))
        ).scalar_one()
        assert member is False, "app role must NOT be able to SET ROLE to the BYPASSRLS role (F085)"


@pytest_asyncio.fixture
async def db_engine_reset() -> AsyncIterator[None]:
    """Dispose the cached engine after each test that uses it, so settings re-read."""
    from app.db.engine import dispose_engine

    yield
    await dispose_engine()


def new_admin_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest_asyncio.fixture
async def two_tenants() -> tuple[uuid.UUID, uuid.UUID]:
    return new_admin_id(), new_admin_id()


@pytest.fixture(autouse=True)
def _no_background_escalation_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """The in-process escalation retry ladder (F065) sleeps 30s/2m on the event loop;
    tests that want it set ``ESCALATION_RETRY_DELAYS`` themselves and drain it."""
    from app.notifications import escalation_dispatch

    monkeypatch.setattr(escalation_dispatch, "ESCALATION_RETRY_DELAYS", ())


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    """Reset the module-global rate-limit store so token buckets / auto-blocks do
    not leak across tests (the limiter is a process singleton in app code)."""
    from app.channels import rate_limit

    rate_limit._store = rate_limit.TokenBucketStore()


@pytest_asyncio.fixture(autouse=True)
async def _fresh_engine_per_test() -> AsyncIterator[None]:
    """Dispose the module-level async engine at the START of every async test.

    Root-cause fix for the intermittent ``got Future ... attached to a different loop`` /
    ``Event loop is closed`` error at fixture setup (seen deterministically on
    test_audit_chain in the full-suite run): the engine is a module-level singleton, but
    ``asyncio_mode=auto`` gives each async test its OWN event loop. An engine built in a
    prior test's loop keeps pool connections bound to that (now closed) loop; the next
    test reusing it via ``get_engine()`` explodes. This autouse fixture runs BEFORE the
    named per-test fixtures (e.g. ``assert_no_bypass``), so every test starts with no
    cached engine and builds a fresh one on its own loop. Idempotent + cheap (no engine
    cached -> no-op).
    """
    from app.db.engine import reset_engine_cache

    # Non-awaiting reset: the stale engine is bound to a prior (closed) loop, so we must
    # NOT await its async dispose here. Just clear the cache; get_engine() rebuilds fresh.
    reset_engine_cache()
    yield


_TRUNCATE_LOCK_TIMEOUT = "30s"
_TENANT_TABLES = (
    "leads, instances, accounts, admin_audit_log, knowledge_sources, "
    "knowledge_chunks, knowledge_entities, knowledge_edges, sessions, transcripts, "
    "session_summaries, instance_connections, escalation_events, "
    "instance_tool_authorizations, conversation_budgets, voice_call_records, "
    "deferred_actions, billing_accounts, counted_sessions, processed_stripe_events, "
    # Not tenant-scoped, but it accumulates across tests just the same.
    "contact_submission"
)


def _truncate_tenant_tables() -> None:
    """The between-tests wipe, as a superuser (bypasses RLS). Runs on a worker thread.

    admin_audit_log carries a statement-level BEFORE TRUNCATE guard (§5.3, migration
    0019) that does not exempt superusers — wiping an audit table is meant to require
    an explicit, visible act. Disabling the guard around the fixture's own TRUNCATE is
    that act, and mirrors what the tamper tests do with the row-level guard.

    TRUNCATE takes an ACCESS EXCLUSIVE lock on every table listed. It waits at most
    ``_TRUNCATE_LOCK_TIMEOUT`` for whoever still holds a row or table lock; past that
    it fails with the list of sessions in the way instead of waiting forever.
    """
    _admin_psql("ALTER TABLE admin_audit_log DISABLE TRIGGER trg_audit_no_truncate", db=TEST_DB)
    try:
        _admin_psql(
            f"SET lock_timeout = '{_TRUNCATE_LOCK_TIMEOUT}'; "
            f"TRUNCATE {_TENANT_TABLES} RESTART IDENTITY CASCADE",
            db=TEST_DB,
        )
    except subprocess.CalledProcessError as exc:
        blockers = _admin_psql(
            "SELECT pid, usename, state, wait_event_type, "
            "now() - xact_start AS xact_age, left(query, 160) AS query "
            "FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
            "ORDER BY xact_start NULLS LAST",
            db=TEST_DB,
        )
        stderr = exc.stderr.decode("utf-8", "replace") if exc.stderr else ""
        raise RuntimeError(
            "the between-tests TRUNCATE could not take its locks within "
            f"{_TRUNCATE_LOCK_TIMEOUT} — a transaction from the test that just ran is "
            f"still open.\npsql: {stderr.strip()}\nsessions on the test database:\n{blockers}"
        ) from exc
    finally:
        _admin_psql("ALTER TABLE admin_audit_log ENABLE TRIGGER trg_audit_no_truncate", db=TEST_DB)


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables() -> AsyncIterator[None]:
    """Truncate tenant tables between tests using a superuser path (bypasses RLS).

    The wipe runs on a WORKER THREAD, never on the event loop. It used to be a
    synchronous ``psql`` call inside this async fixture: TRUNCATE then waited for any
    transaction a coroutine of this same loop still had open, while blocking the very
    loop that coroutine needed to commit — a self-deadlock with no timeout, which hung
    two full-suite CI runs for over half an hour each (round 7, 2026-10-04). Off the
    loop, a straggling coroutine finishes and the wipe proceeds.
    """
    yield
    await asyncio.to_thread(_truncate_tenant_tables)


@pytest_asyncio.fixture(autouse=True)
async def _drain_offturn_notifications() -> AsyncIterator[None]:
    """Escalation notifications run OFF the customer's turn as background tasks that
    start after the turn's commit (round 7 WP-2, item 5). A test that asserts on
    delivery drains them itself; this teardown drains whatever a test left running
    so no task outlives its event loop. Defined after ``_clean_tables`` so it tears
    down first (before the tables are truncated)."""
    from app.notifications import escalation_dispatch

    yield
    # Bounded, and self-diagnosing: a stuck task used to hang the whole job with no
    # output (a CI run sat here for 34 minutes on 2026-10-04). Now it fails THIS
    # test and prints where each stuck task is suspended.
    try:
        await asyncio.wait_for(escalation_dispatch.drain_background_notifications(), timeout=60)
    except TimeoutError:
        stuck = list(escalation_dispatch._BACKGROUND_NOTIFICATIONS)
        report: list[str] = []
        for task in stuck:
            buffer = io.StringIO()
            task.print_stack(file=buffer)
            report.append(f"{task!r}\n{buffer.getvalue()}")
            task.cancel()
        escalation_dispatch._BACKGROUND_NOTIFICATIONS.clear()
        pytest.fail(
            "off-turn escalation notification task(s) did not finish within 60 s:\n"
            + "\n".join(report)
        )
