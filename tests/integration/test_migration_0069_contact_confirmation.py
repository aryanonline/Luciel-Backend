"""Migration 0069 (round 7 WP-4b) applies, rolls back, and re-applies cleanly.

The confirmation table is a tenant table (RLS forced, policy present — the isolation
suite covers that generically), ``escalation_events`` gains the provider message id the
delivery receipt keys on, and ``platform_escalation_backlog()`` grows the persisted
``oldest_queued_at`` that ``/healthz`` reads. Downgrade restores 0057's two-column
function and drops both additions, so a rollback leaves the 0065 shape exactly.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from app.db.session import untenanted_session
from sqlalchemy import text

from tests.conftest import superuser_exec

pytestmark = [pytest.mark.asyncio]

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _alembic(*args: str) -> None:
    """Run alembic the way the ops step does: with the app role a member of
    ``luciel_auth`` for the minutes it runs (``ALTER FUNCTION ... OWNER TO`` needs SET
    on the target role), and the membership revoked again afterwards exactly as
    ``scripts/lock_audit_log.sql`` does after every migration (2026-09-05 audit F085)."""
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


async def _column_exists(table: str, column: str) -> bool:
    async with untenanted_session() as s:
        return (
            await s.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": column},
            )
        ).scalar_one() == 1


async def _table_exists(table: str) -> bool:
    async with untenanted_session() as s:
        return (
            await s.execute(
                text("SELECT count(*) FROM information_schema.tables WHERE table_name = :t"),
                {"t": table},
            )
        ).scalar_one() == 1


async def _backlog_columns() -> list[str]:
    async with untenanted_session() as s:
        result = await s.execute(text("SELECT * FROM platform_escalation_backlog()"))
        return list(result.keys())


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_0069_upgrades_downgrades_and_upgrades_again():
    from app.db.engine import dispose_engine

    assert await _table_exists("escalation_contact_confirmations")
    assert await _column_exists("escalation_events", "delivery_provider_message_id")
    assert await _backlog_columns() == ["queued", "failed_24h", "oldest_queued_at"]

    await dispose_engine()
    _alembic("downgrade", "0065_embedding_verification")
    assert not await _table_exists("escalation_contact_confirmations")
    assert not await _column_exists("escalation_events", "delivery_provider_message_id")
    assert await _backlog_columns() == ["queued", "failed_24h"]

    await dispose_engine()
    _alembic("upgrade", "head")
    assert await _table_exists("escalation_contact_confirmations")
    assert await _column_exists("escalation_events", "delivery_provider_message_id")
    assert await _backlog_columns() == ["queued", "failed_24h", "oldest_queued_at"]
    async with untenanted_session() as s:
        rls = (
            await s.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'escalation_contact_confirmations'"
                )
            )
        ).one()
    assert rls.relrowsecurity and rls.relforcerowsecurity
