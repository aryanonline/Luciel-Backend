"""Migration ``0071_mailbox_poll_drains_quiescing`` upgrades AND downgrades (round 7 WP-5).

The upgrade is exercised by every test in the suite (conftest migrates to head). This
file drives the downgrade against a database that holds a draining Outlook mailbox and
then the upgrade again, asserting at each end:

  * the function BODY is exactly the revision's (``pg_proc.prosrc`` against the
    constants the migration exports) — so the downgrade really restores 0065's text
    and the upgrade changes nothing but the ``mailbox_poll`` branch;
  * the owner is still ``luciel_auth`` and ``luciel_app`` still has EXECUTE (the
    SECURITY DEFINER seam the app role reaches the candidate list through);
  * the live condition: a quiescing row stashed as ``connected`` is a ``mailbox_poll``
    candidate after the upgrade and NOT one after the downgrade — the drain the
    mailbox tenant never reached before this revision.

Alembic needs the migration-time membership in ``luciel_auth`` (the OWNER TO statements
require it); it is granted around each command and revoked again, exactly as the
bootstrap + lock-script pair does in every environment.
"""

from __future__ import annotations

import difflib
import importlib.util
import os
import subprocess
import uuid
from pathlib import Path
from types import ModuleType

import pytest
from app.db.session import tenant_session
from app.jobs import sweeps
from app.models.connection import QUIESCING, InstanceConnection
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import text

from tests.conftest import APP_USER, _admin_psql

REPO_ROOT = Path(__file__).resolve().parents[2]
VERSIONS = REPO_ROOT / "app" / "migrations" / "versions"
MIGRATION = VERSIONS / "0071_mailbox_poll_drains_quiescing.py"
PREVIOUS_DEFINITION = VERSIONS / "0065_embedding_verification.py"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def _norm(sql: str) -> str:
    """SQL text modulo whitespace: the later 0072 restates this body verbatim but
    composes its blank lines differently."""
    return " ".join(sql.split())


async def _function_shape(s) -> tuple[str, str, bool]:
    """(body, owner, luciel_app may EXECUTE) of ``platform_sweep_candidates``."""
    row = (
        await s.execute(
            text(
                "SELECT p.prosrc, pg_get_userbyid(p.proowner), "
                "has_function_privilege('luciel_app', p.oid, 'EXECUTE') "
                "FROM pg_proc p WHERE p.proname = 'platform_sweep_candidates'"
            )
        )
    ).one()
    return str(row[0]), str(row[1]), bool(row[2])


async def _seed_draining_mailbox() -> uuid.UUID:
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
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="email_sender",
                auth_class="oauth_token",
                provider="outlook",
                status=QUIESCING,
                status_stashed="connected",
                non_secret_config={"address": "owner@acme.example", "mode": "byo_mailbox"},
            )
        )
    return admin


def test_the_downgrade_body_is_0065s_text_and_the_upgrade_changes_one_branch():
    """The constants the migration restates ARE the previous definition, byte for
    byte: a drift here would make the downgrade install a function nobody reviewed."""
    this = _load(MIGRATION, "migration_0071")
    previous = _load(PREVIOUS_DEFINITION, "migration_0065")
    assert this.revision == "0071_mailbox_poll_drains_quiescing"
    assert this.BODY_0065 == (
        previous._BODY_0056 + previous._DAILY_BRIEF_BRANCH + previous._KNOWLEDGE_CHUNKS_BRANCH
    )
    assert this.BODY_0071 != this.BODY_0065
    # One branch differs, and it is the mailbox one: everything else is identical. A
    # positional diff, not a set difference — the removed line also appears verbatim
    # in the knowledge_sync branch, which a set difference would let cancel out.
    hunk = [
        line
        for line in difflib.unified_diff(
            this.BODY_0065.splitlines(), this.BODY_0071.splitlines(), lineterm="", n=0
        )
        if line[:1] in "-+" and not line.startswith(("---", "+++"))
    ]
    assert hunk == [
        "-           AND c.status = 'connected'",
        "+           AND (c.status = 'connected'",
        "+                OR (c.status = 'quiescing' AND c.status_stashed = 'connected'))",
    ]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_0071_downgrades_and_upgrades_again():
    this = _load(MIGRATION, "migration_0071")
    admin = await _seed_draining_mailbox()

    async with tenant_session(admin) as s:
        body, owner, executable = await _function_shape(s)
    # At head the body is 0072's: this revision's text plus the stale-escalation branch
    # 0072 appends (it chains after 0071 and restates this body, drain clause included).
    assert _norm(this.BODY_0071) in _norm(body)
    assert (owner, executable) == ("luciel_auth", True)
    assert admin in await sweeps.sweep_candidates("mailbox_poll"), (
        "at head a draining mailbox stashed as connected is polled"
    )

    _alembic("downgrade", str(this.down_revision))
    try:
        async with tenant_session(admin) as s:
            body, owner, executable = await _function_shape(s)
        assert _norm(body) == _norm(this.BODY_0065), "the downgrade restores 0065's body"
        assert (owner, executable) == ("luciel_auth", True)
        # The live condition this revision fixes: before it, the tenant fell out of
        # the candidate list the moment the delete landed.
        assert admin not in await sweeps.sweep_candidates("mailbox_poll")
    finally:
        _alembic("upgrade", "head")

    async with tenant_session(admin) as s:
        body, owner, executable = await _function_shape(s)
    assert _norm(this.BODY_0071) in _norm(body)
    assert (owner, executable) == ("luciel_auth", True)
    assert admin in await sweeps.sweep_candidates("mailbox_poll")
