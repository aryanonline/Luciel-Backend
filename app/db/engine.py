"""Async SQLAlchemy engine + sessionmaker."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            echo=False,
        )
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(), expire_on_commit=False, class_=AsyncSession
        )
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


def reset_engine_cache() -> None:
    """Drop the cached engine/sessionmaker WITHOUT awaiting an async dispose.

    Used by the test harness at the start of each async test: the previous test's engine
    is bound to a now-closed event loop, so ``await _engine.dispose()`` would itself touch
    that dead loop. Clearing the module globals lets ``get_engine()`` build a fresh engine
    on the current loop; the orphaned engine's pool is closed by GC/loop teardown. Safe
    because tests never share a live engine across loops by design.
    """
    global _engine, _sessionmaker
    _engine = None
    _sessionmaker = None
