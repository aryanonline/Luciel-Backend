"""The between-tests wipe can neither deadlock the suite nor wait forever (round 7).

``tests/conftest.py`` truncates every tenant table after each test. Its statements
take strong locks (an ALTER TABLE on the audit table, TRUNCATE on everything), so
they wait for any transaction still open on those tables. Three full-suite CI runs
sat in that wait for over half an hour each (2026-10-04/05):

* first as a synchronous ``psql`` call inside an async fixture — when the open
  transaction belonged to a coroutine of the same event loop, the wipe blocked the
  loop that coroutine needed to commit, a self-deadlock with no timeout;
* then because only the TRUNCATE carried a lock timeout: the ALTER TABLE that
  disables the audit guard waited with none, and an uncommitted AUDIT ROW from a
  task finishing just after its test held exactly the lock it needed.

Pinned here, against the real database:

* a transaction that is still finishing on this loop no longer deadlocks the wipe;
* an audit row still being written no longer hangs it either;
* a transaction that will never finish is terminated and recorded as that test's
  leak, so the run goes on and the summary names the culprit;
* a statement that is still running after every attempt fails the wipe, naming
  the session in the way, instead of hanging the job;
* no superuser psql call can wait forever.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
from app.audit import log as audit
from app.db.session import tenant_session
from app.models.audit import ActorType, AdminAuditLog
from app.models.instance import Account, AccountState
from sqlalchemy import func, select, text

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


async def _audit_row(session: object, admin: uuid.UUID) -> None:
    await audit.emit(
        session,  # type: ignore[arg-type]
        admin_id=admin,
        event_type="instance_configured",
        actor_type=ActorType.admin_user,
        payload={"from": "test_between_tests_cleanup"},
    )


async def _wipe(timeout: float = 25) -> None:
    # On the event loop this call could never return when the loop itself holds the
    # blocking transaction: TRUNCATE waits for the writer, and the writer needs the
    # loop the call is blocking. Off the loop, both proceed.
    await asyncio.wait_for(asyncio.to_thread(conftest._truncate_tenant_tables), timeout=timeout)


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
    await _wipe()
    await writer

    async with tenant_session(admin) as s:
        assert (await s.execute(select(func.count()).select_from(Account))).scalar_one() == 0


@pytest.mark.usefixtures("db_engine_reset")
async def test_an_audit_row_still_being_written_does_not_hang_the_wipe() -> None:
    """The third CI hang: the wipe's trigger ALTER on admin_audit_log queued behind an
    uncommitted audit row with no lock timeout at all."""
    admin = uuid.uuid4()
    holding = asyncio.Event()

    async def _slow_auditor() -> None:
        async with tenant_session(admin) as s:
            await _audit_row(s, admin)
            await s.flush()
            holding.set()
            await asyncio.sleep(1.0)

    writer = asyncio.get_running_loop().create_task(_slow_auditor())
    await holding.wait()
    await _wipe()
    await writer

    async with tenant_session(admin) as s:
        count = await s.execute(select(func.count()).select_from(AdminAuditLog))
        assert count.scalar_one() == 0


@pytest.mark.usefixtures("db_engine_reset")
async def test_a_transaction_that_never_finishes_is_terminated_and_recorded_as_a_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conftest, "_TRUNCATE_LOCK_TIMEOUT", "1s")
    monkeypatch.setattr(conftest, "_TRUNCATE_ATTEMPTS", 4)
    leaks: list[str] = []
    monkeypatch.setattr(conftest, "_LEAKED_TRANSACTIONS", leaks)
    admin = uuid.uuid4()
    holding = asyncio.Event()
    release = asyncio.Event()

    async def _stuck_auditor() -> None:
        async with tenant_session(admin) as s:
            await _audit_row(s, admin)
            await s.flush()
            holding.set()
            await release.wait()

    writer = asyncio.get_running_loop().create_task(_stuck_auditor())
    await holding.wait()
    try:
        await _wipe(timeout=20)
    finally:
        release.set()
    # The leak is named against THIS test, with what the session was last doing.
    assert len(leaks) == 1, leaks
    assert "test_a_transaction_that_never_finishes_is_terminated" in leaks[0]
    assert "idle for" in leaks[0]
    # Its session is gone: the writer cannot commit on a terminated backend.
    with pytest.raises(Exception, match="(?i)connection|terminat|closed"):
        await writer


@pytest.mark.usefixtures("db_engine_reset")
async def test_a_runaway_statement_fails_the_wipe_naming_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conftest, "_TRUNCATE_LOCK_TIMEOUT", "1s")
    monkeypatch.setattr(conftest, "_TRUNCATE_ATTEMPTS", 3)
    admin = uuid.uuid4()
    holding = asyncio.Event()

    async def _runaway() -> None:
        async with tenant_session(admin) as s:
            await s.execute(text("LOCK TABLE accounts IN ACCESS EXCLUSIVE MODE"))
            holding.set()
            # Still ACTIVE (not idle) through every attempt — never terminated, so the
            # wipe has to give up and say who is in the way.
            await s.execute(text("SELECT pg_sleep(25)"))

    task = asyncio.get_running_loop().create_task(_runaway())
    await holding.wait()
    try:
        with pytest.raises(RuntimeError) as failure:
            await _wipe(timeout=20)
        message = str(failure.value)
        assert "could not take its locks within 1s on 3 attempts" in message
        assert "sessions on the test database" in message
        assert "pg_sleep" in message
    finally:
        task.cancel()
        with contextlib.suppress(BaseException):
            await task


async def test_the_superuser_psql_backstop_refuses_to_wait_forever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conftest, "_PSQL_TIMEOUT_SECONDS", 2)
    with pytest.raises(RuntimeError, match="did not return within 2 s"):
        await asyncio.to_thread(conftest._admin_psql, "SELECT pg_sleep(6)", conftest.TEST_DB)
