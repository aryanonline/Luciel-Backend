"""The weekly audit_retention sweep, run the way the container runs it (round 7 fact
check, 2026-10-25).

``scripts/run-sweep.sh audit_retention`` ends in ``python -m app.jobs audit_retention``,
which is ``app.jobs.__main__.main(["audit_retention"])``. Every Sunday from 2026-09-27 to
2026-10-25 that process died on its first query (/luciel/dev/sweep, 06:30Z):

    File "/app/app/jobs/__main__.py", line 112, in main
      result: object = asyncio.run(_run_sweep(name))
    ...
    File "/app/app/jobs/sweeps.py", line 125, in sweep_candidates
    ...
    RuntimeError: Task <Task pending name='Task-5' coro=<_run_sweep() ...>> got Future
    <Future pending cb=[BaseProtocol._on_waiter_completed()]> attached to a different loop

The audit sweep alone preflighted the audit-log lock under one ``asyncio.run`` and swept
under a second. The engine is a module-level singleton: the preflight's connection went
back into the pool bound to the first loop, and the sweep's checkout pre-pinged it from
the second.

These tests drive ``main`` on a worker thread with no engine cached, as a fresh process
starts, so each ``asyncio.run`` inside it owns its loop exactly as in the container.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db import engine as db_engine
from app.db.session import tenant_session
from app.jobs import __main__ as jobs_main
from app.jobs import sweeps
from app.jobs.sweeps import AUDIT_RETENTION
from app.models.audit import ActorType, AdminAuditLog
from app.models.instance import Account, AccountState
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

AGED = timedelta(days=400)


async def _seed_tenant_with_an_aged_row() -> uuid.UUID:
    """An account older than a year carrying one audit row past the retention."""
    admin_id = uuid.uuid4()
    async with tenant_session(admin_id) as s:
        s.add(
            Account(
                id=admin_id,
                admin_id=admin_id,
                user_id=admin_id,
                email=f"{admin_id}@x.com",
                email_lower=f"{admin_id}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
                created_at=datetime.now(UTC) - AGED - timedelta(days=5),
            )
        )
    async with tenant_session(admin_id) as s:
        s.add(
            AdminAuditLog(
                event_id=uuid.uuid4(),
                admin_id=admin_id,
                actor_type=ActorType.platform,
                event_type="login_succeeded",
                event_payload={"aged": True},
                created_at=datetime.now(UTC) - AGED,
                prev_hash=None,
            )
        )
    return admin_id


async def _event_types(admin_id: uuid.UUID) -> list[str]:
    async with tenant_session(admin_id) as s:
        rows = await s.execute(
            select(AdminAuditLog.event_type)
            .where(AdminAuditLog.admin_id == admin_id)
            .order_by(AdminAuditLog.seq.asc())
        )
        return list(rows.scalars().all())


async def test_the_container_entry_point_prunes_the_aged_rows_and_exits_zero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    admin_id = await _seed_tenant_with_an_aged_row()
    # A fresh process has no engine: drop this loop's before the "process" starts.
    await db_engine.dispose_engine()

    code = await asyncio.wait_for(asyncio.to_thread(jobs_main.main, [AUDIT_RETENTION]), 60)

    out = capsys.readouterr()
    assert "different loop" not in out.err
    assert code == 0, out.err
    line = json.loads(out.out.strip().splitlines()[-1])
    assert line["sweep"] == AUDIT_RETENTION
    assert str(admin_id) in line["result"]
    # The process disposed the engine inside its own loop before that loop closed.
    assert db_engine._engine is None
    assert await _event_types(admin_id) == ["data_retention_hard_delete"]


async def test_the_seedtest_shape_of_many_loops_each_disposing_runs_against_the_real_db() -> None:
    """scripts/seed_and_livetest.py's asyncio.run blocks each open a session and dispose
    the engine in a finally, also when a sync caller built the engine first."""
    await db_engine.dispose_engine()

    async def _block() -> None:
        try:
            assert await jobs_main._preflight_audit_lock() == []
        finally:
            await db_engine.dispose_engine()

    def _harness() -> None:
        db_engine.get_engine()
        for _ in range(3):
            asyncio.run(_block())

    try:
        await asyncio.wait_for(asyncio.to_thread(_harness), 60)
        assert db_engine._engine is None and db_engine._engine_loop is None
    finally:
        db_engine.reset_engine_cache()


async def test_the_old_two_loop_sequencing_is_refused_by_name_not_four_frames_down() -> None:
    """The exact pre-fix sequence (preflight under one loop, sweep under another) now
    fails at the engine with an error that says what is wrong, instead of an asyncpg
    pre-ping deep inside the first query."""
    await db_engine.dispose_engine()

    def _old_main() -> None:
        assert asyncio.run(jobs_main._preflight_audit_lock()) == []
        asyncio.run(sweeps.run_audit_retention())

    try:
        with pytest.raises(db_engine.EngineLoopMismatchError, match="ONE asyncio.run"):
            await asyncio.wait_for(asyncio.to_thread(_old_main), 60)
    finally:
        # The first loop's engine is orphaned (its loop is closed): drop it unawaited.
        db_engine.reset_engine_cache()
