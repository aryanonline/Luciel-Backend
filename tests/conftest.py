"""Test fixtures: a real Postgres test database with RLS, and a Redis flush.

The isolation suite MUST run against a real Postgres with the app connecting as a
NON-superuser, NON-BYPASSRLS role — otherwise RLS is silently inert and the tests
prove nothing. conftest points the app at ``luciel_test`` (created/migrated here)
and asserts the connection role cannot bypass RLS before any test runs.
"""

from __future__ import annotations

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


def _admin_psql(sql: str, db: str = "postgres") -> None:
    if _CI_PG:
        env = dict(os.environ, PGPASSWORD=_SUPERUSER_PW)
        subprocess.run(
            [
                "psql",
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
        subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-d", db, "-v", "ON_ERROR_STOP=1", "-c", sql],
            check=True,
            capture_output=True,
        )


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
                "psql",
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
                "psql",
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


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables() -> AsyncIterator[None]:
    """Truncate tenant tables between tests using a superuser path (bypasses RLS)."""
    yield
    # Cleanup after each test. Use the postgres superuser to truncate regardless of RLS.
    # admin_audit_log carries a statement-level BEFORE TRUNCATE guard (§5.3, migration
    # 0019) that does not exempt superusers — wiping an audit table is meant to require
    # an explicit, visible act. Disabling the guard around the fixture's own TRUNCATE is
    # that act, and mirrors what the tamper tests do with the row-level guard.
    _admin_psql(
        "ALTER TABLE admin_audit_log DISABLE TRIGGER trg_audit_no_truncate",
        db=TEST_DB,
    )
    _admin_psql(
        "TRUNCATE leads, instances, accounts, admin_audit_log, knowledge_sources, "
        "knowledge_chunks, knowledge_entities, knowledge_edges, sessions, transcripts, "
        "session_summaries, instance_connections, escalation_events, "
        "instance_tool_authorizations, conversation_budgets, voice_call_records, "
        "deferred_actions, billing_accounts, counted_sessions, processed_stripe_events, "
        # Not tenant-scoped, but it accumulates across tests just the same.
        "contact_submission "
        "RESTART IDENTITY CASCADE",
        db=TEST_DB,
    )
    _admin_psql(
        "ALTER TABLE admin_audit_log ENABLE TRIGGER trg_audit_no_truncate",
        db=TEST_DB,
    )
