"""Migration 0076 (round 7 M10) applies, rolls back, and re-applies cleanly.

``transcripts.delivery_provider_message_id`` is a nullable, schema-only addition (no
backfill, no row touched) with a partial index for the carrier-receipt lookup.
Downgrade drops both and leaves the previous revision's shape exactly.
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
_REVISION = "0076_m10_followups"
_INDEX = "ix_transcripts_delivery_provider_message_id"


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


async def _column(table: str, column: str) -> tuple[str, int | None, str] | None:
    async with untenanted_session() as s:
        row = (
            await s.execute(
                text(
                    "SELECT data_type, character_maximum_length, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_name = :t AND column_name = :c"
                ),
                {"t": table, "c": column},
            )
        ).one_or_none()
    return (row[0], row[1], row[2]) if row is not None else None


async def _index_exists() -> bool:
    async with untenanted_session() as s:
        return (
            await s.execute(
                text("SELECT 1 FROM pg_indexes WHERE tablename = 'transcripts' AND indexname = :i"),
                {"i": _INDEX},
            )
        ).first() is not None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_0076_upgrades_downgrades_and_upgrades_again():
    from app.db.engine import dispose_engine

    column = ("character varying", 64, "YES")
    assert await _column("transcripts", "delivery_provider_message_id") == column
    assert await _index_exists()

    await dispose_engine()
    # Relative to this revision, so the test still steps back exactly one migration
    # after the chain is re-ordered at merge.
    _alembic("downgrade", f"{_REVISION}-1")
    assert await _column("transcripts", "delivery_provider_message_id") is None
    assert not await _index_exists()
    # The 0066 delivery-outcome columns beside it are untouched by the rollback.
    assert await _column("transcripts", "delivery_status") is not None

    await dispose_engine()
    _alembic("upgrade", "head")
    assert await _column("transcripts", "delivery_provider_message_id") == column
    assert await _index_exists()
