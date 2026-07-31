"""Layer-3 tenant isolation: per-request ``app.admin_id``.

A tenant-scoped DB session sets ``app.admin_id`` exactly once, at the start of
the request's transaction, from the authenticated subject (admin JWT in the
control plane, or embed-key/webhook resolution in the data plane). It is set with
``SET LOCAL`` so it is scoped to the transaction and cannot leak across pooled
connections, and it is never read from a query parameter or request body
(Architecture §3.7.2b Layer 3, Space Instructions §3.1).

Two entry points:
  - ``tenant_session(admin_id)``: async context manager used by request handlers
    and workers. Opens a transaction, sets the GUC, yields the session.
  - ``set_admin_id(session, admin_id)``: low-level helper (used by tests and the
    context manager) that emits the ``SET LOCAL``.

There is deliberately NO API to change ``app.admin_id`` mid-transaction — the
value is fixed for the connection's transaction lifetime.
"""

from __future__ import annotations

import uuid
import zlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.engine import get_sessionmaker


async def set_admin_id(session: AsyncSession, admin_id: uuid.UUID) -> None:
    """Set the per-request tenant GUC inside the current transaction.

    Uses ``set_config(key, value, is_local := true)`` with a bound parameter so
    the admin_id is never string-interpolated into SQL. ``is_local = true`` makes
    it transaction-scoped (equivalent to SET LOCAL).
    """
    await session.execute(
        text("SELECT set_config('app.admin_id', :aid, true)"),
        {"aid": str(admin_id)},
    )


async def claim_sweep_lock(session: AsyncSession, kind: str, admin_id: uuid.UUID) -> bool:
    """Claim the (sweep kind, tenant) pair for THIS transaction, or report it taken.

    Sweeps had no mutual exclusion of any kind (P2-7). Two overlapping runs — a retried
    scheduled invocation, a manual run during an incident, two tasks after a deploy —
    would each read the same candidate list and each do the work: duplicate audit rows
    for one grace expiry, duplicate Secrets Manager deletes, a deferred item delivered
    twice. Nothing in the schema forbids any of that, so nothing caught it.

    A transaction-scoped advisory lock is the right shape because the unit of work is
    already one transaction per tenant: Postgres releases it on commit OR rollback, so
    a crashed sweep cannot wedge a tenant out of every future run — which a table-based
    "claimed_at" flag would do until someone noticed.

    ``try`` rather than a blocking wait: the loser has nothing useful to wait for, since
    by the time the lock frees the winner has already done the work.
    """
    keys = _lock_keys(kind, admin_id)
    return bool(
        (
            await session.execute(
                text("SELECT pg_try_advisory_xact_lock(:k1, :k2)"), {"k1": keys[0], "k2": keys[1]}
            )
        ).scalar_one()
    )


def _lock_keys(kind: str, admin_id: uuid.UUID) -> tuple[int, int]:
    """Two int4 lock keys. Derived in Python so the pair is stable across releases —
    ``hashtext`` is explicitly not guaranteed stable across Postgres versions."""
    return (
        zlib.crc32(kind.encode()) - 2**31,
        int.from_bytes(admin_id.bytes[:4], "big") - 2**31,
    )


@asynccontextmanager
async def tenant_session(admin_id: uuid.UUID) -> AsyncIterator[AsyncSession]:
    """Yield a tenant-scoped session with ``app.admin_id`` pinned for the txn."""
    sm = get_sessionmaker()
    async with sm() as session:
        async with session.begin():
            await set_admin_id(session, admin_id)
            yield session


@asynccontextmanager
async def untenanted_session() -> AsyncIterator[AsyncSession]:
    """A session with NO tenant GUC set.

    Used only by plane-level auth resolution (e.g. looking up an account by email
    at login, or resolving an embed key to its tenant) — operations that run
    before a tenant subject exists. These paths must never read tenant-scoped
    rows; RLS will return zero rows for any tenant table when the GUC is unset,
    which is the safe default. Account/embed-key lookups use dedicated,
    non-RLS-bypassing queries on the auth tables only.
    """
    sm = get_sessionmaker()
    async with sm() as session:
        async with session.begin():
            yield session
