"""Async SQLAlchemy engine + sessionmaker.

The engine is a process-wide singleton, and an asyncpg pool belongs to the event loop
that opened its connections: a connection checked in under one loop and checked out
under another fails its pre-ping with ``got Future ... attached to a different loop``.
So a process that touches the database runs ONE event loop for its whole life (the
sweep CLI, ``app/jobs/__main__.py``) or disposes the engine inside each loop before
that loop closes (the SeedTest harness). ``get_engine`` refuses a second loop by name
instead of letting the pool fail four frames down (the weekly ``audit_retention`` sweep
crashed exactly that way every Sunday from 2026-09-27 to 2026-10-25).
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None
# The loop the cached engine was built under (None when it was built outside one).
_engine_loop: asyncio.AbstractEventLoop | None = None


class EngineLoopMismatchError(RuntimeError):
    """The cached engine was built under an event loop other than the running one."""


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _refuse_a_second_loop() -> None:
    running = _running_loop()
    if _engine is not None and _engine_loop is not None and running is not None:
        if running is not _engine_loop:
            raise EngineLoopMismatchError(
                "the database engine was built under a different event loop than the "
                "running one; its pooled connections belong to that loop. Run the whole "
                "process under ONE asyncio.run(), or `await dispose_engine()` before each "
                "loop closes (app/db/engine.py)."
            )


def get_engine() -> AsyncEngine:
    global _engine, _engine_loop
    _refuse_a_second_loop()
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            echo=False,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_recycle=settings.db_pool_recycle_seconds,
            # asyncpg applies these on every new connection: a hard statement
            # deadline (WP7) and a name the DBA can see in pg_stat_activity.
            connect_args={
                "server_settings": {
                    "statement_timeout": str(settings.db_statement_timeout_ms),
                    "application_name": f"luciel-{os.environ.get('LUCIEL_PLANE', 'local')}",
                }
            },
        )
        _engine_loop = _running_loop()
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    _refuse_a_second_loop()
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker, _engine_loop
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None
    _engine_loop = None


def reset_engine_cache() -> None:
    """Drop the cached engine/sessionmaker WITHOUT awaiting an async dispose.

    Used by the test harness at the start of each async test: the previous test's engine
    is bound to a now-closed event loop, so ``await _engine.dispose()`` would itself touch
    that dead loop. Clearing the module globals lets ``get_engine()`` build a fresh engine
    on the current loop; the orphaned engine's pool is closed by GC/loop teardown. Safe
    because tests never share a live engine across loops by design.
    """
    global _engine, _sessionmaker, _engine_loop
    _engine = None
    _sessionmaker = None
    _engine_loop = None
