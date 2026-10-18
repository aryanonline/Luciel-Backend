"""Migration 0072 (round 7 delivery hardening) applies, rolls back, and re-applies.

The deferred sweep's candidate list and the escalation backlog both learn about the
rows the queue cannot see: a ``pending`` escalation whose notification task died and a
``sent`` one whose delivery receipt never came. Downgrade restores 0065's candidate
body and 0069's backlog function exactly, so a rollback leaves the 0070 shape.
"""

from __future__ import annotations

import os
import re
import subprocess
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db.session import tenant_session, untenanted_session
from app.jobs import sweeps
from app.models.conversation import EscalationEvent
from app.models.instance import Account, AccountState, Instance, LucielState
from app.notifications.escalation_dispatch import PENDING_STALE_AFTER, SENT_UNCONFIRMED_AFTER
from app.runtime.integration_status import escalation_backlog
from sqlalchemy import text

from tests.conftest import superuser_exec

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MIGRATION = os.path.join(
    _REPO_ROOT, "app", "migrations", "versions", "0072_escalation_delivery_hardening.py"
)


def _alembic(*args: str) -> None:
    """Run alembic the way the ops step does (see test_migration_0069_contact_confirmation)."""
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


async def _seed(admin: uuid.UUID, iid: uuid.UUID) -> None:
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=uuid.uuid4(),
                email=f"{admin.hex[:8]}@acme.example",
                email_lower=f"{admin.hex[:8]}@acme.example",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(Instance(id=iid, admin_id=admin, name="Acme", state=LucielState.active))


async def _escalation(
    admin: uuid.UUID, iid: uuid.UUID, *, status: str, age: timedelta, sid: str | None = None
) -> None:
    async with tenant_session(admin) as s:
        s.add(
            EscalationEvent(
                admin_id=admin,
                instance_id=iid,
                session_id=uuid.uuid4(),
                signal="high_value_lead",
                gate="outcome",
                fired_at=datetime.now(UTC) - age,
                delivery_status=status,
                delivery_provider_message_id=sid,
            )
        )


async def _function_source(name: str) -> str:
    async with untenanted_session() as s:
        return (
            await s.execute(
                text("SELECT pg_get_functiondef(p.oid) FROM pg_proc p WHERE p.proname = :n"),
                {"n": name},
            )
        ).scalar_one()


def test_the_sql_bounds_are_copies_of_the_dispatcher_constants() -> None:
    """The migration's intervals must say what ``escalation_dispatch`` says, or the
    sweep would be handed tenants it then finds nothing to do for (or miss some)."""
    source = open(_MIGRATION, encoding="utf-8").read()
    pending = re.search(r'PENDING_STALE_INTERVAL = "(\d+) minutes"', source)
    sent = re.search(r'SENT_UNCONFIRMED_INTERVAL = "(\d+) minutes"', source)
    assert pending and sent
    assert timedelta(minutes=int(pending.group(1))) == PENDING_STALE_AFTER
    assert timedelta(minutes=int(sent.group(1))) == SENT_UNCONFIRMED_AFTER


@pytest.mark.asyncio
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_0072_upgrades_downgrades_and_upgrades_again():
    from app.db.engine import dispose_engine

    lost, young, unreceipted, idle = (uuid.uuid4() for _ in range(4))
    for admin in (lost, young, unreceipted, idle):
        await _seed(admin, uuid.uuid4())
    lost_iid = uuid.uuid4()
    await _escalation(
        lost, lost_iid, status="pending", age=PENDING_STALE_AFTER + timedelta(minutes=1)
    )
    await _escalation(young, uuid.uuid4(), status="pending", age=timedelta(minutes=1))
    await _escalation(
        unreceipted,
        uuid.uuid4(),
        status="sent",
        age=SENT_UNCONFIRMED_AFTER + timedelta(minutes=1),
        sid="SM" + "a" * 32,
    )
    await _escalation(idle, uuid.uuid4(), status="delivered", age=timedelta(hours=2))

    # Upgraded (the suite's head): the lost and unreceipted tenants are deferred
    # candidates, the young and settled ones are not; the backlog counts the lost page
    # and dates the backlog from it.
    candidates = set(await sweeps.sweep_candidates(sweeps.DEFERRED))
    assert {lost, unreceipted} <= candidates
    assert not {young, idle} & candidates
    backlog = await escalation_backlog()
    assert backlog is not None and backlog["queued"] == 1
    assert backlog["oldestQueuedAt"] is not None
    assert datetime.now(UTC) - backlog["oldestQueuedAt"] > PENDING_STALE_AFTER
    assert "pending" in await _function_source("platform_escalation_backlog")

    await dispose_engine()
    _alembic("downgrade", "0070_knowledge_answer_flags")
    assert not {lost, unreceipted, young, idle} & set(
        await sweeps.sweep_candidates(sweeps.DEFERRED)
    )
    backlog = await escalation_backlog()
    assert backlog is not None and backlog["queued"] == 0 and backlog["oldestQueuedAt"] is None
    assert "pending" not in await _function_source("platform_escalation_backlog")
    assert "escalation_events" not in await _function_source("platform_sweep_candidates")

    await dispose_engine()
    _alembic("upgrade", "head")
    assert {lost, unreceipted} <= set(await sweeps.sweep_candidates(sweeps.DEFERRED))
    backlog = await escalation_backlog()
    assert backlog is not None and backlog["queued"] == 1
    # Both functions stay the narrow SECURITY DEFINER seams owned by luciel_auth.
    async with untenanted_session() as s:
        owners = (
            await s.execute(
                text(
                    "SELECT p.proname, r.rolname, p.prosecdef FROM pg_proc p "
                    "JOIN pg_roles r ON r.oid = p.proowner "
                    "WHERE p.proname IN "
                    "('platform_sweep_candidates', 'platform_escalation_backlog')"
                )
            )
        ).all()
    assert {(row[0], row[1], row[2]) for row in owners} == {
        ("platform_sweep_candidates", "luciel_auth", True),
        ("platform_escalation_backlog", "luciel_auth", True),
    }
