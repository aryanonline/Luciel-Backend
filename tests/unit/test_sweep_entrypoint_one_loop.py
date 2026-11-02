"""Every sweep the container runs uses ONE event loop for the whole process (round 7
fact check, 2026-10-25: the weekly audit_retention sweep preflighted under one
``asyncio.run`` and swept under a second, and the shared engine's pool died on the
loop switch every Sunday from 2026-09-27).

``python -m app.jobs <sweep>`` is ``main([sweep])``. These tests run it for every
name in ``SWEEPS`` with the database work stood in, and pin the shape that makes the
failure impossible: one ``asyncio.run``, the preflight and the sweep on the same loop,
the engine disposed inside that loop before it closes. And the engine itself refuses a
second loop by name.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from app.db import engine as db_engine
from app.jobs import __main__ as jobs_main
from app.jobs.sweeps import AUDIT_RETENTION, SWEEPS


@pytest.mark.parametrize("name", sorted(SWEEPS))
def test_one_loop_runs_the_preflight_the_sweep_and_the_dispose(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    loops: dict[str, asyncio.AbstractEventLoop] = {}
    runs: list[object] = []
    real_run = asyncio.run

    def _counting_run(coro: Any, **kw: Any) -> Any:
        runs.append(coro)
        return real_run(coro, **kw)

    async def _preflight() -> list[str]:
        loops["preflight"] = asyncio.get_running_loop()
        return []

    async def _sweep() -> list[str]:
        loops["sweep"] = asyncio.get_running_loop()
        return []

    async def _dispose() -> None:
        loops["dispose"] = asyncio.get_running_loop()

    monkeypatch.setattr(jobs_main.asyncio, "run", _counting_run)
    monkeypatch.setattr(jobs_main, "_preflight_audit_lock", _preflight)
    monkeypatch.setitem(SWEEPS, name, _sweep)
    monkeypatch.setattr(db_engine, "dispose_engine", _dispose)

    assert jobs_main.main([name]) == 0

    assert len(runs) == 1, "the sweep process must run under exactly one event loop"
    expected = {"sweep", "dispose"} | ({"preflight"} if name == AUDIT_RETENTION else set())
    assert set(loops) == expected
    assert len({id(loop) for loop in loops.values()}) == 1


def test_a_missing_audit_lock_still_refuses_with_the_remedy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    swept: list[bool] = []

    async def _preflight() -> list[str]:
        return ["audit_retention_prune exists"]

    async def _sweep() -> list[str]:
        swept.append(True)
        return []

    async def _dispose() -> None:
        return None

    monkeypatch.setattr(jobs_main, "_preflight_audit_lock", _preflight)
    monkeypatch.setitem(SWEEPS, AUDIT_RETENTION, _sweep)
    monkeypatch.setattr(db_engine, "dispose_engine", _dispose)

    assert jobs_main.main([AUDIT_RETENTION]) == 1
    err = capsys.readouterr().err
    assert "refusing to run: the audit-log lock is not applied" in err
    assert "  FAIL audit_retention_prune exists\n" in err
    assert "lock_audit_log.sql" in err
    assert swept == []


def test_a_crashed_sweep_logs_an_error_line_the_metric_filter_counts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The SweepErrors filter matches a standalone ERROR token. A bare uncaught
    traceback has none; the 2026-09-27..10-25 crashes alarmed only because SQLAlchemy's
    pool logged its own ERROR while tearing down the dead loop's connection."""

    async def _sweep() -> list[str]:
        raise RuntimeError("attached to a different loop")

    async def _dispose() -> None:
        return None

    monkeypatch.setitem(SWEEPS, AUDIT_RETENTION, _sweep)
    monkeypatch.setattr(jobs_main, "_preflight_audit_lock", lambda: asyncio.sleep(0, []))
    monkeypatch.setattr(db_engine, "dispose_engine", _dispose)

    with caplog.at_level(logging.ERROR, logger="app.jobs"):
        assert jobs_main.main([AUDIT_RETENTION]) == 1
    [record] = [r for r in caplog.records if "sweep_failed" in r.getMessage()]
    assert record.levelno == logging.ERROR
    assert f"sweep={AUDIT_RETENTION}" in record.getMessage()
    # The format main() installs puts the level token first, which is what the
    # filter matches on in the container's log stream.
    formatted = logging.Formatter("%(levelname)s %(name)s %(message)s").format(record)
    assert formatted.startswith("ERROR app.jobs sweep_failed")


def test_the_engine_refuses_a_second_loop_by_name() -> None:
    db_engine.reset_engine_cache()

    async def _build() -> None:
        db_engine.get_sessionmaker()

    try:
        asyncio.run(_build())
        with pytest.raises(db_engine.EngineLoopMismatchError, match="different event loop"):
            asyncio.run(_build())

        # Disposing inside a loop releases the binding: the next loop builds its own.
        async def _dispose_then_build() -> None:
            db_engine.reset_engine_cache()
            db_engine.get_sessionmaker()
            await db_engine.dispose_engine()

        asyncio.run(_dispose_then_build())
        asyncio.run(_build())
    finally:
        db_engine.reset_engine_cache()


def test_an_engine_built_outside_any_loop_is_refused_on_the_second_loop_that_uses_it() -> None:
    """A sync caller can build the engine with no running loop. Its pool still fills under
    the first loop that uses it, so a second loop is the shape that crashed every Sunday:
    it binds to that first loop and refuses the second."""
    db_engine.reset_engine_cache()

    async def _use() -> None:
        db_engine.get_sessionmaker()

    try:
        db_engine.get_engine()
        asyncio.run(_use())
        with pytest.raises(db_engine.EngineLoopMismatchError, match="different event loop"):
            asyncio.run(_use())
    finally:
        db_engine.reset_engine_cache()


def test_a_loop_that_disposes_in_a_finally_leaves_the_next_loop_free() -> None:
    """scripts/seed_and_livetest.py's shape: many asyncio.run blocks, each disposing the
    engine in a finally. dispose_engine must release the bound loop, however the engine
    was built, or the second block of the D10 gate would be refused."""
    db_engine.reset_engine_cache()

    async def _block() -> None:
        try:
            db_engine.get_sessionmaker()
        finally:
            await db_engine.dispose_engine()

    try:
        asyncio.run(_block())
        assert db_engine._engine_loop is None
        db_engine.get_engine()  # built outside a loop between two blocks
        asyncio.run(_block())
        asyncio.run(_block())
        assert db_engine._engine is None and db_engine._engine_loop is None
    finally:
        db_engine.reset_engine_cache()


def test_a_dispose_that_raises_still_releases_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    db_engine.reset_engine_cache()

    async def _refused(_self: object) -> None:
        raise OSError("connection reset while closing")

    async def _block() -> None:
        db_engine.get_engine()
        await db_engine.dispose_engine()

    monkeypatch.setattr(db_engine.AsyncEngine, "dispose", _refused)
    try:
        with pytest.raises(OSError, match="connection reset"):
            asyncio.run(_block())
        assert db_engine._engine is None and db_engine._engine_loop is None
    finally:
        db_engine.reset_engine_cache()
