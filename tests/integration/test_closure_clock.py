"""The closure clock is the CURRENT closure, and the closure records what it took
(round 7 WP-5 / owner decision D5).

Before D5 an account closed once and stayed closed, so the purge read its 30-day clock
from the FIRST ``account_closure_initiated`` audit row. D5 lets the owner reopen and —
later — close again; read from the first row, the second closure would be purged the
moment it happened (or days early). ``accounts.closed_at`` (migration 0068) is the
clock now: stamped by ``cascade.close_account``, shared by the reduced closed-account
surface and the purge, with the audit row as the fallback for rows closed before the
column existed. The closure also writes what it did to the Luciel onto its audit row,
because the reopen restores exactly that and nothing the owner had deleted on purpose.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.audit import log as audit
from app.db.session import tenant_session
from app.lifecycle import cascade
from app.lifecycle.close_purge import (
    closure_started_at,
    purge_closed_account_if_due,
    reopen_deadline,
)
from app.models.audit import ActorType
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]


async def _seed(luciel: LucielState | None) -> uuid.UUID:
    admin = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin}@x.com",
                email_lower=f"{admin}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        if luciel is not None:
            s.add(
                Instance(
                    id=uuid.uuid4(),
                    admin_id=admin,
                    name="L",
                    state=luciel,
                    embed_key_public_id=f"vm_live_{uuid.uuid4().hex[:10]}",
                    grace_window_started_at=(
                        datetime.now(UTC) if luciel == LucielState.luciel_grace_window else None
                    ),
                    channels=[],
                    tools=[],
                    escalation={},
                    personality={},
                )
            )
    return admin


async def _account(admin: uuid.UUID) -> Account | None:
    async with tenant_session(admin) as s:
        return (await s.execute(select(Account).where(Account.id == admin))).scalar_one_or_none()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize(
    ("before", "deleted_by_closure"),
    [
        (LucielState.active, True),
        (LucielState.paused, True),
        # The owner had already deleted the Luciel: the closure took nothing more.
        (LucielState.luciel_grace_window, False),
        (None, False),
    ],
)
async def test_closure_stamps_the_clock_and_records_what_it_did_to_the_luciel(
    before: LucielState | None, deleted_by_closure: bool
):
    admin = await _seed(before)
    started = datetime.now(UTC)
    async with tenant_session(admin) as s:
        await cascade.close_account(s, admin, admin)

    acct = await _account(admin)
    assert acct is not None and acct.state == AccountState.closed
    assert acct.closed_at is not None
    assert abs(acct.closed_at - started) < timedelta(seconds=30)
    async with tenant_session(admin) as s:
        record = await cascade.closure_record(s, admin)
        assert await closure_started_at(s, admin) == acct.closed_at
    assert record == {
        "luciel_state_before": before.value if before else None,
        "luciel_deleted_by_closure": deleted_by_closure,
    }


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_repeated_close_does_not_restart_the_thirty_days():
    admin = await _seed(LucielState.active)
    async with tenant_session(admin) as s:
        await cascade.close_account(s, admin, admin)
    first = (await _account(admin)).closed_at  # type: ignore[union-attr]
    async with tenant_session(admin) as s:
        await cascade.close_account(s, admin, admin)
    assert (await _account(admin)).closed_at == first  # type: ignore[union-attr]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_reclosed_account_is_purged_on_its_current_closure_not_its_first():
    """Closed once (the audit row below), reopened, closed again ten days later
    (``closed_at``). Thirty-five days after the FIRST closure is only twenty-five into
    the current one: the shell — and the owner's right to reopen — must survive."""
    admin = await _seed(None)
    async with tenant_session(admin) as s:
        first = (
            await audit.emit(
                s,
                admin_id=admin,
                actor_type=ActorType.admin_user,
                event_type="account_closure_initiated",
            )
        ).created_at
        acct = (await s.execute(select(Account).where(Account.id == admin))).scalar_one()
        acct.state = AccountState.closed
        acct.closed_at = first + timedelta(days=10)

    async with tenant_session(admin) as s:
        assert await closure_started_at(s, admin) == first + timedelta(days=10)
        assert (
            await purge_closed_account_if_due(s, admin_id=admin, now=first + timedelta(days=35))
            is False
        ), "purged on the first closure's clock — 25 days into the current one"
    assert await _account(admin) is not None

    async with tenant_session(admin) as s:
        assert (
            await purge_closed_account_if_due(s, admin_id=admin, now=first + timedelta(days=41))
            is True
        )
    assert await _account(admin) is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_reopen_deadline_is_the_purge_deadline():
    """One function: the last instant the reduced surface offers a reopen is the first
    instant the purge may run. A closure at T is NOT purged at T+30d minus a second."""
    admin = await _seed(None)
    async with tenant_session(admin) as s:
        await cascade.close_account(s, admin, admin)
    closed_at = (await _account(admin)).closed_at  # type: ignore[union-attr]
    deadline = reopen_deadline(closed_at)
    assert deadline - closed_at == timedelta(days=30)
    async with tenant_session(admin) as s:
        assert (
            await purge_closed_account_if_due(
                s, admin_id=admin, now=deadline - timedelta(seconds=1)
            )
            is False
        )
    async with tenant_session(admin) as s:
        assert await purge_closed_account_if_due(s, admin_id=admin, now=deadline) is True
