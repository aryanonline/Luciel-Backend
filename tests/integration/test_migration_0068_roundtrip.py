"""Migration ``0068_quiescing_and_export_sweep`` upgrades AND downgrades (round 7 WP-5).

The upgrade is exercised by every test in the suite (conftest migrates to head). This
file drives the downgrade against a database that holds a draining row, and then the
upgrade again, asserting the three shapes the app relies on at each end:

  * the 3-arg ``channel_resolve_destination`` (default admits ``quiescing``) — the app
    calls it with three arguments, so a 2-arg function would fail every inbound;
  * the ``uq_sms_destination`` predicate that lets another tenant claim a draining
    row's number during the grace window;
  * ``accounts.closed_at`` (owner decision D5).

The downgrade's row flip (``quiescing`` → ``revoked``) is asserted through a tenant
session: it runs as the BYPASSRLS role for one statement because ``instance_connections``
is FORCE RLS and alembic runs as the app role — a flip that silently matched zero rows
would leave the older code with a status it has never heard of.

Alembic needs the migration-time membership in ``luciel_auth`` (the OWNER TO statements
require it); it is granted around each command and revoked again, exactly as the
bootstrap + lock-script pair does in every environment.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import uuid
from pathlib import Path

import pytest
from app.channels import routing
from app.db.session import tenant_session
from app.models.connection import QUIESCING, InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import select, text

from tests.conftest import APP_USER, _admin_psql

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = REPO_ROOT / "app" / "migrations" / "versions" / "0068_quiescing_and_export_sweep.py"
NUM = "+16045550177"


def _down_revision() -> str:
    """Read the migration's own ``down_revision`` so the test follows a re-chain."""
    spec = importlib.util.spec_from_file_location("migration_0068", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0068_quiescing_and_export_sweep"
    return str(module.down_revision)


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


async def _resolver_arguments(s) -> list[str]:
    return list(
        (
            await s.execute(
                text(
                    "SELECT pg_get_function_arguments(p.oid) FROM pg_proc p "
                    "WHERE p.proname = 'channel_resolve_destination'"
                )
            )
        )
        .scalars()
        .all()
    )


async def _sms_index_def(s) -> str:
    return str(
        (
            await s.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_sms_destination'")
            )
        ).scalar_one()
    )


async def _email_index_def(s) -> str:
    """``uq_email_destination`` is written by 0067 and re-stated here: chained after
    it at merge, 0068 must make a draining mailbox release its structural claim too."""
    return str(
        (
            await s.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'uq_email_destination'")
            )
        ).scalar_one()
    )


async def _closed_at_exists(s) -> bool:
    return (
        await s.execute(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_name = 'accounts' AND column_name = 'closed_at'"
            )
        )
    ).scalar_one() == 1


async def _seed_draining_tenant() -> tuple[uuid.UUID, uuid.UUID]:
    admin, iid, cid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
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
                state=LucielState.luciel_grace_window,
                embed_key_public_id=f"vm_live_{admin.hex[:8]}",
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        s.add(
            InstanceConnection(
                connection_id=cid,
                admin_id=admin,
                instance_id=iid,
                connection_type="sms_sender",
                auth_class="api_key",
                provider="twilio",
                status=QUIESCING,
                status_stashed="connected",
                non_secret_config={"destination": NUM},
            )
        )
    return admin, cid


async def _status(admin: uuid.UUID, cid: uuid.UUID) -> str:
    async with tenant_session(admin) as s:
        return (
            await s.execute(
                select(InstanceConnection.status).where(InstanceConnection.connection_id == cid)
            )
        ).scalar_one()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_0068_downgrades_and_upgrades_again():
    admin, cid = await _seed_draining_tenant()
    async with tenant_session(admin) as s:
        assert "p_include_quiescing boolean DEFAULT true" in (await _resolver_arguments(s))[0]
        assert "'quiescing'" in await _sms_index_def(s)
        assert "'quiescing'" in await _email_index_def(s)
        assert await _closed_at_exists(s)

    _alembic("downgrade", _down_revision())
    try:
        assert await _status(admin, cid) == "revoked", "the flip must actually reach the rows"
        async with tenant_session(admin) as s:
            assert (await _resolver_arguments(s)) == ["p_ct text, p_dest text"]
            index_def = await _sms_index_def(s)
            assert "'quiescing'" not in index_def and "'revoked'" in index_def
            email_def = await _email_index_def(s)
            assert "'quiescing'" not in email_def and "'revoked'" in email_def
            assert not await _closed_at_exists(s)
    finally:
        _alembic("upgrade", "head")

    async with tenant_session(admin) as s:
        args = await _resolver_arguments(s)
        assert len(args) == 1 and "p_include_quiescing boolean DEFAULT true" in args[0]
        assert "'quiescing'" in await _sms_index_def(s)
        assert "'quiescing'" in await _email_index_def(s)
        assert await _closed_at_exists(s)
        # The app's 3-arg call works again, and the row the downgrade retired stays
        # retired (revoked is terminal; nothing re-derives a drain from it).
        row = (
            await s.execute(
                select(InstanceConnection).where(InstanceConnection.connection_id == cid)
            )
        ).scalar_one()
        row.status = QUIESCING
        await s.flush()
        assert (await routing.resolve_destination(s, "sms_sender", NUM)).admin_id == admin
        assert await routing.owner_of_destination(s, "sms_sender", NUM) is None
