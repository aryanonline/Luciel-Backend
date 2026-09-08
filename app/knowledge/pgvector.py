"""pgvector version truth (round 6 WP-G, F044).

The HNSW index is one global index over every tenant's chunks; the tenant filter is
applied AFTER the approximate nearest-neighbour walk. With the default ``ef_search``
(40) a small tenant sitting beside a large one can get fewer than k rows back — the
walk's 40 candidates are mostly the big tenant's. pgvector 0.8 added iterative scans,
which keep walking until the filtered result set is full. ``retrieve`` sets both
``hnsw.ef_search`` and (when the extension is new enough) ``hnsw.iterative_scan`` per
transaction; ``/healthz`` reports an extension older than the pin as degraded.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

#: The version docker-compose.yml and CI pin (pgvector/pgvector:0.8.0-pg17); the first
#: with ``hnsw.iterative_scan``.
MIN_PGVECTOR: tuple[int, int, int] = (0, 8, 0)

_cached: tuple[int, int, int] | None = None
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?")


def parse_version(raw: str | None) -> tuple[int, int, int] | None:
    """``"0.8.6"`` → (0, 8, 6); None for anything unparseable."""
    m = _VERSION_RE.match((raw or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)


def supports_iterative_scan(version: tuple[int, int, int] | None) -> bool:
    return version is not None and version >= MIN_PGVECTOR


async def installed_version(session: AsyncSession) -> tuple[int, int, int] | None:
    """The installed extension version, cached per process after the first read."""
    global _cached
    if _cached is not None:
        return _cached
    row = (
        await session.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'"))
    ).scalar_one_or_none()
    parsed = parse_version(str(row) if row is not None else None)
    if parsed is not None:
        _cached = parsed
    return parsed


def reset_cache() -> None:
    global _cached
    _cached = None


def too_old_reason(version: tuple[int, int, int] | None) -> str | None:
    """``pgvector_too_old:<ver>`` for /healthz when the extension predates the pin."""
    if version is None:
        return "pgvector_too_old:unknown"
    if version < MIN_PGVECTOR:
        return "pgvector_too_old:" + ".".join(str(x) for x in version)
    return None


def search_settings_sql(k: int, version: tuple[int, int, int] | None) -> list[str]:
    """The SET LOCAL statements ``retrieve`` runs before the ANN query (F044).

    ``ef_search`` = max(40, 4k) widens the candidate walk with k; ``iterative_scan =
    relaxed_order`` (0.8+) keeps walking until the tenant filter is satisfied. Both are
    transaction-local, so nothing leaks to the next request on the pooled connection.
    """
    ef = max(40, 4 * int(k))
    stmts = [f"SET LOCAL hnsw.ef_search = {ef}"]
    if supports_iterative_scan(version):
        stmts.append("SET LOCAL hnsw.iterative_scan = 'relaxed_order'")
    return stmts
