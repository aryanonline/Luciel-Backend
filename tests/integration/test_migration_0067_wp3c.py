"""Migration 0067_lead_claims_and_identity — upgrade, downgrade, and the fail-loud
data preconditions, run through the real alembic CLI against the test database
(round 7 WP-3c items 10, 13, 14).

The migration is the STRUCTURAL half of two owner rulings (2026-09-14): email
claims are case-insensitive, and one embed key belongs to one instance. Like
0045, it must never skip its index to get past bad data — a silently-skipped
index would hide a tenant-crossing hazard behind a successful-looking deploy — so
this test seeds exactly the conditions the duplicate sweep exists to clear and
asserts the migration REFUSES with a count, then succeeds once they are cleared,
then round-trips a downgrade.

It leaves the database at head (the ``finally`` re-upgrades after cleanup).
"""

from __future__ import annotations

import os
import subprocess
import uuid

import pytest
from app.channels import routing
from app.db.engine import dispose_engine
from app.db.session import tenant_session, untenanted_session
from app.models.connection import InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import delete, select, text, update

from tests.conftest import APP_USER, superuser_exec

pytestmark = [pytest.mark.asyncio]

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 0067's own down_revision since the round-7 Phase 2 chain was linearised
# (0065 -> 0066 -> 0067 -> 0068 -> 0069 -> 0070): the downgrade also unwinds the
# migrations chained after it.
PREVIOUS = "0066_takeover_delivery_truth"
REVISION = "0067_lead_claims_and_identity"
MAILBOX_CAPS = "Owner@Fabrikam.Example"
MAILBOX = "owner@fabrikam.example"
SOLO_CAPS = "Sales@Contoso.Example"
SHARED_KEY = f"vm_live_dup{uuid.uuid4().hex[:6]}"
# Role membership is CLUSTER-wide, and conftest's own bootstrap (grant → alembic →
# lock) toggles it at every session start: a second test session on the same
# Postgres can revoke the membership between this test's grant and the migration's
# use of it. Migrations are transactional, so that one specific refusal is retried
# rather than reported as a migration defect. Any other failure is returned as is.
_ROLE_RACE_SIGNATURE = 'set role "luciel_auth"'
_ROLE_RACE_ATTEMPTS = 4


def _alembic_once(args: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    """One alembic run bracketed the way ``scripts/migrate.sh`` brackets it.

    The app role is a member of the BYPASSRLS ``luciel_auth`` role ONLY while a
    migration runs: ``bootstrap_roles.sql`` grants it before alembic and
    ``lock_audit_log.sql`` revokes it after (audit F085). 0067 needs that window
    twice over — to re-own its SECURITY DEFINER lookup and to read across tenants
    for its preconditions — and the test body itself keeps running as a role that
    cannot bypass RLS."""
    superuser_exec(f"GRANT luciel_auth TO {APP_USER}")
    try:
        return subprocess.run(  # noqa: S603 — fixed argv, test-only
            ["alembic", *args],
            cwd=REPO_ROOT,
            env=dict(os.environ),
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        superuser_exec(f"REVOKE luciel_auth FROM {APP_USER}")


async def _alembic(*args: str) -> subprocess.CompletedProcess[str]:
    """Run alembic as the app role (same env, repo cwd), with the app's pooled
    connections disposed first so DDL never waits on an idle session."""
    await dispose_engine()
    proc = _alembic_once(args)
    for _ in range(_ROLE_RACE_ATTEMPTS - 1):
        if proc.returncode == 0 or _ROLE_RACE_SIGNATURE not in proc.stderr.lower():
            break
        proc = _alembic_once(args)
    return proc


async def _index_def(name: str) -> str | None:
    async with untenanted_session() as s:
        return (
            await s.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"), {"n": name}
            )
        ).scalar_one_or_none()


async def _function_exists(name: str) -> bool:
    async with untenanted_session() as s:
        return bool(
            (
                await s.execute(
                    text("SELECT count(*) FROM pg_proc WHERE proname = :n"), {"n": name}
                )
            ).scalar_one()
        )


async def _seed(admin: uuid.UUID, *, embed_key: str) -> uuid.UUID:
    iid = uuid.uuid4()
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
                name="Migrate Co",
                state=LucielState.active,
                embed_key_public_id=embed_key,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        await s.flush()
    return iid


async def _mailbox_row(admin: uuid.UUID, iid: uuid.UUID, destination: str) -> None:
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="email_sender",
                auth_class="oauth_token",
                provider="outlook",
                status="connected",
                non_secret_config={
                    "address": destination,
                    "mode": "byo_mailbox",
                    "destination": destination,
                },
            )
        )
        await s.flush()


async def _destination(admin: uuid.UUID) -> str | None:
    async with tenant_session(admin) as s:
        row = (
            await s.execute(
                select(InstanceConnection).where(
                    InstanceConnection.admin_id == admin,
                    InstanceConnection.connection_type == "email_sender",
                )
            )
        ).scalar_one()
        return (row.non_secret_config or {}).get("destination")


async def _cleanup(admins: list[uuid.UUID]) -> None:
    for admin in admins:
        async with tenant_session(admin) as s:
            await s.execute(delete(InstanceConnection).where(InstanceConnection.admin_id == admin))
            await s.execute(delete(Instance).where(Instance.admin_id == admin))
            await s.execute(delete(Account).where(Account.admin_id == admin))


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_0067_refuses_duplicates_loudly_then_upgrades_and_downgrades_cleanly():
    a, b, solo, c, d = (uuid.uuid4() for _ in range(5))
    admins = [a, b, solo, c, d]
    try:
        # Tenants are seeded at HEAD: the ORM models describe the head schema
        # (``accounts.closed_at`` belongs to 0068, later in the chain), so an Account
        # cannot be inserted through them once the downgrade below has run. The bad
        # data is then shaped with UPDATEs and connection rows, which touch only
        # columns that exist at every revision involved.
        a_iid = await _seed(a, embed_key=f"vm_live_{a.hex[:10]}")
        b_iid = await _seed(b, embed_key=f"vm_live_{b.hex[:10]}")
        solo_iid = await _seed(solo, embed_key=f"vm_live_{solo.hex[:10]}")
        await _seed(c, embed_key=f"vm_live_{c.hex[:10]}")
        await _seed(d, embed_key=f"vm_live_{d.hex[:10]}")

        # --- downgrade: the structures the migration adds are gone --------------- #
        down = await _alembic("downgrade", PREVIOUS)
        assert down.returncode == 0, down.stderr
        assert await _index_def("uq_email_destination") is None
        embed_idx = await _index_def("ix_instances_embed_key")
        assert embed_idx is not None and "UNIQUE" not in embed_idx.upper()
        assert not await _function_exists("connection_config_holders")

        # --- the bad data the sweep exists to clear ------------------------------ #
        for tenant in (c, d):  # two instances, one embed key
            async with tenant_session(tenant) as s:
                await s.execute(
                    update(Instance)
                    .where(Instance.admin_id == tenant)
                    .values(embed_key_public_id=SHARED_KEY)
                )
        await _mailbox_row(a, a_iid, MAILBOX_CAPS)  # one mailbox, two live claims,
        await _mailbox_row(b, b_iid, MAILBOX)  # differing only in case
        await _mailbox_row(solo, solo_iid, SOLO_CAPS)  # unique, but typed with capitals

        # --- precondition 1: duplicate (case-folded) email claims → refused ------ #
        up = await _alembic("upgrade", "head")
        assert up.returncode != 0
        assert f"{REVISION} refused" in up.stderr
        assert "1 email_sender destination(s)" in up.stderr
        assert await _index_def("uq_email_destination") is None  # nothing half-applied
        assert await _destination(solo) == SOLO_CAPS  # the lower-casing rolled back too

        # The sweep releases the duplicate (a revoked row is out of the index's scope).
        async with tenant_session(b) as s:
            await s.execute(
                update(InstanceConnection)
                .where(InstanceConnection.admin_id == b)
                .values(status="revoked")
            )

        # --- precondition 2: two instances on one embed key → refused ------------ #
        up = await _alembic("upgrade", "head")
        assert up.returncode != 0
        assert f"{REVISION} refused" in up.stderr
        assert "1 embed key(s)" in up.stderr
        assert await _index_def("uq_email_destination") is None

        async with tenant_session(d) as s:
            await s.execute(
                update(Instance)
                .where(Instance.admin_id == d)
                .values(embed_key_public_id=f"vm_live_{d.hex[:10]}")
            )

        # --- clean data: the upgrade applies everything ---------------------------- #
        up = await _alembic("upgrade", "head")
        assert up.returncode == 0, up.stderr
        email_idx = await _index_def("uq_email_destination")
        assert email_idx is not None
        assert "lower(" in email_idx and "WHERE" in email_idx and "email_sender" in email_idx
        embed_idx = await _index_def("ix_instances_embed_key")
        assert embed_idx is not None and "UNIQUE" in embed_idx.upper()
        assert await _function_exists("connection_config_holders")
        # The one-time lower-casing rewrote the rows that predate the ruling.
        assert await _destination(a) == MAILBOX
        assert await _destination(solo) == SOLO_CAPS.lower()
        # The narrow cross-tenant lookup answers through RLS: the live holder only.
        async with tenant_session(solo) as s:
            holders = await routing.holders_of_connection_config(
                s, "email_sender", "destination", MAILBOX
            )
        assert holders == [a]

        # --- round trip: downgrade is clean, and a re-upgrade of clean data is too - #
        down = await _alembic("downgrade", PREVIOUS)
        assert down.returncode == 0, down.stderr
        assert await _index_def("uq_email_destination") is None
        assert not await _function_exists("connection_config_holders")
        up = await _alembic("upgrade", "head")
        assert up.returncode == 0, up.stderr
        assert await _index_def("uq_email_destination") is not None
    finally:
        await _cleanup(admins)
        final = await _alembic("upgrade", "head")
        assert final.returncode == 0, final.stderr
