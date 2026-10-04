"""The between-tests wipe can neither deadlock the suite nor wait forever (round 7).

``tests/conftest.py`` truncates every tenant table after each test. TRUNCATE needs an
ACCESS EXCLUSIVE lock on each of them, so it waits for any transaction still open on
those tables. It used to run as a synchronous ``psql`` call inside an async fixture:
when the open transaction belonged to a coroutine of the same event loop, the wipe
blocked the loop that coroutine needed to commit — a self-deadlock with no timeout.
Two full-suite CI runs sat in it for over half an hour each (2026-10-04).

Two properties are pinned here, against the real database:

* a transaction that is still finishing on this loop no longer deadlocks the wipe —
  the wipe runs on a worker thread, the coroutine commits, the wipe proceeds;
* a transaction that never finishes fails the wipe within its lock timeout, naming
  the sessions in the way, instead of hanging the job.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from app.db.session import tenant_session
from app.models.instance import Account, AccountState
from sqlalchemy import func, select

from tests import conftest

pytestmark = [pytest.mark.asyncio]


def _account(admin: uuid.UUID) -> Account:
    return Account(
        id=admin,
        admin_id=admin,
        user_id=admin,
        email=f"{admin.hex[:8]}@cleanup.test",
        email_lower=f"{admin.hex[:8]}@cleanup.test",
        password_hash="x",
        state=AccountState.verified,
        email_verified=True,
    )


@pytest.mark.usefixtures("db_engine_reset")
async def test_a_transaction_still_finishing_on_this_loop_does_not_deadlock_the_wipe() -> None:
    admin = uuid.uuid4()
    holding = asyncio.Event()

    async def _slow_writer() -> None:
        # Holds a row lock on ``accounts`` for a moment, then commits — exactly what a
        # background task finishing just after its test does.
        async with tenant_session(admin) as s:
            s.add(_account(admin))
            await s.flush()
            holding.set()
            await asyncio.sleep(1.0)

    writer = asyncio.get_running_loop().create_task(_slow_writer())
    await holding.wait()
    # On the event loop this call could never return: TRUNCATE waits for the writer,
    # and the writer needs the loop the call is blocking.
    await asyncio.wait_for(asyncio.to_thread(conftest._truncate_tenant_tables), timeout=25)
    await writer

    async with tenant_session(admin) as s:
        assert (await s.execute(select(func.count()).select_from(Account))).scalar_one() == 0


@pytest.mark.usefixtures("db_engine_reset")
async def test_a_transaction_that_never_finishes_fails_the_wipe_with_the_blockers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conftest, "_TRUNCATE_LOCK_TIMEOUT", "2s")
    admin = uuid.uuid4()
    holding = asyncio.Event()
    release = asyncio.Event()

    async def _stuck_writer() -> None:
        async with tenant_session(admin) as s:
            s.add(_account(admin))
            await s.flush()
            holding.set()
            await release.wait()

    writer = asyncio.get_running_loop().create_task(_stuck_writer())
    await holding.wait()
    try:
        with pytest.raises(RuntimeError) as failure:
            await asyncio.wait_for(asyncio.to_thread(conftest._truncate_tenant_tables), timeout=25)
        message = str(failure.value)
        assert "could not take its locks within 2s" in message
        assert "sessions on the test database" in message
        # The session in the way is listed with its state, so the leak is findable.
        assert "idle in transaction" in message
    finally:
        release.set()
        await writer
