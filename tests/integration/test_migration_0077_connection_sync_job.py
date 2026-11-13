"""Migration 0077 (round 7 walk run 8) applies, rolls back, and re-applies cleanly.

``instance_connections.sync_job`` is a nullable JSONB, schema-only (no backfill, no row
touched): a connection that never ran a sync job reads NULL. Downgrade drops it and
leaves the previous revision's shape exactly.
"""

from __future__ import annotations

import os
import subprocess

import pytest
from app.db.session import untenanted_session
from sqlalchemy import text

from tests.conftest import superuser_exec

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_REVISION = "0077_connection_sync_job"


def _alembic(*args: str) -> None:
    """Run alembic the way the ops step does (the app role a member of
    ``luciel_auth`` only for the run, revoked again afterwards)."""
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


async def _column(table: str, column: str) -> tuple[str, str] | None:
    async with untenanted_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT data_type, is_nullable FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": column},
            )
        ).one_or_none()
    return (row[0], row[1]) if row is not None else None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_0077_upgrades_downgrades_and_upgrades_again():
    from app.db.engine import dispose_engine

    assert await _column("instance_connections", "sync_job") == ("jsonb", "YES")

    await dispose_engine()
    # Relative to this revision, so the test still steps back exactly one migration
    # after the chain is re-ordered at merge.
    _alembic("downgrade", f"{_REVISION}-1")
    assert await _column("instance_connections", "sync_job") is None
    # The columns beside it are untouched by the rollback.
    assert await _column("instance_connections", "status_detail_stashed") is not None

    await dispose_engine()
    _alembic("upgrade", "head")
    assert await _column("instance_connections", "sync_job") == ("jsonb", "YES")
