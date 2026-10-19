"""Migration 0072 (round 7 delivery hardening) applies, rolls back, and re-applies.

The deferred sweep's candidate list and the escalation backlog both learn about the
rows the queue cannot see: a ``pending`` escalation whose notification task died and a
``sent`` one whose delivery receipt never came (its window counted from the new
``sent_at`` column, ``fired_at`` for rows stamped before it). Downgrade restores 0071's candidate
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
    admin: uuid.UUID,
    iid: uuid.UUID,
    *,
    status: str,
    age: timedelta,
    sid: str | None = None,
    sent_age: timedelta | None = None,
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
                sent_at=(datetime.now(UTC) - sent_age) if sent_age is not None else None,
            )
        )


async def _has_sent_at() -> bool:
    async with untenanted_session() as s:
        return (
            await s.execute(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'escalation_events' AND column_name = 'sent_at'"
                )
            )
        ).scalar_one() == 1


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


def test_the_candidate_body_is_0071s_text_plus_the_stale_branch() -> None:
    """0072 chains after 0071 and restates the same function. Its base body must be
    0071's (the mailbox_poll branch that admits a draining mailbox), not 0065's: a
    restatement from the older text would silently undo the mailbox drain of a
    deleted Luciel on upgrade AND on downgrade."""
    import importlib.util

    def _load(path, name):
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    this = _load(_MIGRATION, "migration_0072")
    previous = _load(
        os.path.join(os.path.dirname(_MIGRATION), "0071_mailbox_poll_drains_quiescing.py"),
        "migration_0071",
    )
    norm = lambda sql: " ".join(sql.split())  # noqa: E731 — modulo whitespace
    assert norm(this._BODY_0071) == norm(previous.BODY_0071)
    assert "c.status_stashed = 'connected'" in this._BODY_0071
    assert this._STALE_ESCALATIONS_BRANCH.strip().startswith("UNION")


@pytest.mark.asyncio
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_0072_upgrades_downgrades_and_upgrades_again():
    from app.db.engine import dispose_engine

    lost, young, unreceipted, idle, just_sent = (uuid.uuid4() for _ in range(5))
    for admin in (lost, young, unreceipted, idle, just_sent):
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
    # An hour-old escalation whose page went out a minute ago (a replay): the receipt
    # window runs from ``sent_at``, so it is not a candidate yet. ``unreceipted`` above
    # has no ``sent_at`` (stamped before the column existed) and falls back to fired_at.
    await _escalation(
        just_sent,
        uuid.uuid4(),
        status="sent",
        age=timedelta(hours=1),
        sid="SM" + "b" * 32,
        sent_age=timedelta(minutes=1),
    )

    # Upgraded (the suite's head): the lost and unreceipted tenants are deferred
    # candidates, the young and settled ones are not; the backlog counts the lost page
    # and dates the backlog from it.
    candidates = set(await sweeps.sweep_candidates(sweeps.DEFERRED))
    assert {lost, unreceipted} <= candidates
    assert not {young, idle, just_sent} & candidates
    assert await _has_sent_at()
    backlog = await escalation_backlog()
    assert backlog is not None and backlog["queued"] == 1
    assert backlog["oldestQueuedAt"] is not None
    assert datetime.now(UTC) - backlog["oldestQueuedAt"] > PENDING_STALE_AFTER
    assert "pending" in await _function_source("platform_escalation_backlog")

    await dispose_engine()
    _alembic("downgrade", "0070_knowledge_answer_flags")
    assert not {lost, unreceipted, young, idle, just_sent} & set(
        await sweeps.sweep_candidates(sweeps.DEFERRED)
    )
    assert not await _has_sent_at()
    backlog = await escalation_backlog()
    assert backlog is not None and backlog["queued"] == 0 and backlog["oldestQueuedAt"] is None
    assert "pending" not in await _function_source("platform_escalation_backlog")
    assert "escalation_events" not in await _function_source("platform_sweep_candidates")

    await dispose_engine()
    _alembic("upgrade", "head")
    assert await _has_sent_at()
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
