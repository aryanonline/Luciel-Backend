"""Migration ``0070_knowledge_answer_flags``: the paused-source backfill touches rows.

``knowledge_sources`` is FORCE RLS and alembic runs as the app role with no tenant
pinned, so a plain UPDATE in a migration matches ZERO rows and reports success. 0070's
"sources already paused start their clock NOW" was exactly that silent no-op (round 7
review): a source revoked before the deploy would have kept answering forever instead
of stopping after the seven days owner decision D10 promises. The backfill now runs as
the narrow BYPASSRLS role for that one statement, like 0067 and 0068 do.

This test downgrades to 0069, seeds a paused source and a healthy one, upgrades
again, and reads the clock back through the tenant's own session.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest
from app.db.session import tenant_session
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import text

from tests.conftest import APP_USER, _admin_psql

pytestmark = [pytest.mark.asyncio]

REPO_ROOT = Path(__file__).resolve().parents[2]
PREVIOUS = "0069_sms_contact_confirmation"


def _alembic(*args: str) -> None:
    _admin_psql(f"GRANT luciel_auth TO {APP_USER}")
    try:
        subprocess.run(
            ["alembic", *args],
            check=True,
            capture_output=True,
            cwd=REPO_ROOT,
            env=dict(os.environ),
        )
    finally:
        _admin_psql(f"REVOKE luciel_auth FROM {APP_USER}")


async def _seed_tenant() -> tuple[uuid.UUID, uuid.UUID]:
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin.hex[:8]}@x.com",
                email_lower=f"{admin.hex[:8]}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="Acme",
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{admin.hex[:8]}",
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
    return admin, iid


async def _insert_source(admin: uuid.UUID, iid: uuid.UUID, sync_status: str | None) -> uuid.UUID:
    # Raw SQL: at 0069 the ORM model's ``paused_since`` column does not exist yet.
    sid = uuid.uuid4()
    async with tenant_session(admin) as s:
        await s.execute(
            text(
                "INSERT INTO knowledge_sources (id, admin_id, instance_id, name, origin, "
                "ingestion_status, size_bytes, sync_status, last_updated_at) "
                "VALUES (:id, :admin, :iid, :name, 'sync', 'completed', 10, :status, now())"
            ),
            {
                "id": sid,
                "admin": admin,
                "iid": iid,
                "name": f"doc-{sid.hex[:6]}",
                "status": sync_status,
            },
        )
    return sid


async def _paused_since(admin: uuid.UUID, sid: uuid.UUID) -> object:
    async with tenant_session(admin) as s:
        return (
            await s.execute(
                text("SELECT paused_since FROM knowledge_sources WHERE id = :id"), {"id": sid}
            )
        ).scalar_one()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_0070_starts_the_clock_of_a_source_that_was_already_paused():
    admin, iid = await _seed_tenant()
    _alembic("downgrade", PREVIOUS)
    try:
        paused = await _insert_source(admin, iid, "paused_reconnect_needed")
        healthy = await _insert_source(admin, iid, "synced")
    finally:
        _alembic("upgrade", "head")

    assert await _paused_since(admin, paused) is not None, (
        "the backfill matched no rows: it is running as the app role under FORCE RLS"
    )
    assert await _paused_since(admin, healthy) is None

    # The grant used for the backfill is gone again.
    async with tenant_session(admin) as s:
        allowed = (
            await s.execute(
                text(
                    "SELECT has_table_privilege('luciel_auth', 'knowledge_sources', 'UPDATE') "
                    "OR has_table_privilege('luciel_auth', 'knowledge_sources', 'SELECT')"
                )
            )
        ).scalar_one()
    assert allowed is False
