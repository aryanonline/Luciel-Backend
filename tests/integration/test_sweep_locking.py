"""Two sweeps over the same tenant must not both do the work (P2-7).

Nothing in the schema forbids a double sweep: the cascade's audit rows have no
uniqueness constraint, the Secrets Manager deletes are fire-and-forget, and a deferred
item can be delivered twice. So the only thing standing between "the scheduler retried
an invocation" and "the customer got the same follow-up twice" is the claim these tests
cover.

The lock is transaction-scoped on purpose, and the rollback test is the reason: a
``claimed_at`` column would survive a crashed sweep and wedge that tenant out of every
future run, which is a worse failure than the one being fixed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db.session import claim_sweep_lock, tenant_session
from app.jobs import sweeps
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


async def _seed_grace_tenant(admin_id: uuid.UUID) -> None:
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
            )
        )
        s.add(
            Instance(
                id=uuid.uuid4(),
                admin_id=admin_id,
                name="L",
                state=LucielState.luciel_grace_window,
                grace_window_started_at=NOW - timedelta(days=31),
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )


async def _instance_exists(admin_id: uuid.UUID) -> bool:
    async with tenant_session(admin_id) as s:
        row = (
            await s.execute(select(Instance).where(Instance.admin_id == admin_id))
        ).scalar_one_or_none()
    return row is not None


async def test_a_tenant_another_run_is_already_expiring_is_skipped_not_double_deleted() -> None:
    """The case the audit names: concurrent ``grace_expiry`` runs double-emit audit rows
    and double-call Secrets Manager, because the cascade is not idempotent in its
    side effects even though it is idempotent in its row deletes."""
    admin_id = uuid.uuid4()
    await _seed_grace_tenant(admin_id)

    # Stand in for the run that got there first: hold its transaction open across the
    # second run, exactly as a real in-flight cascade would.
    async with tenant_session(admin_id) as holder:
        assert await claim_sweep_lock(holder, sweeps.GRACE_EXPIRY, admin_id) is True
        assert await sweeps.run_grace_purge(now=NOW) == {}
        assert await _instance_exists(admin_id) is True

    # The holder committed without doing the work, so the next run must still find it —
    # a skip is a deferral to the winner, not a permanent exemption.
    assert str(admin_id) in await sweeps.run_grace_purge(now=NOW)
    assert await _instance_exists(admin_id) is False


async def test_a_crashed_sweep_does_not_wedge_the_tenant_out_of_future_runs() -> None:
    """Postgres drops an xact lock on ROLLBACK as well as COMMIT, which is the whole
    reason this is an advisory lock and not a claimed_at column."""
    admin_id = uuid.uuid4()
    await _seed_grace_tenant(admin_id)

    with pytest.raises(RuntimeError):
        async with tenant_session(admin_id) as s:
            assert await claim_sweep_lock(s, sweeps.GRACE_EXPIRY, admin_id) is True
            raise RuntimeError("cascade blew up mid-tenant")

    assert str(admin_id) in await sweeps.run_grace_purge(now=NOW)


async def test_the_claim_is_per_tenant_and_per_kind_so_one_holder_stalls_nothing_else() -> None:
    """A single lock for "the sweep" would serialize the whole platform behind one slow
    tenant; a single lock per tenant would let the retention run block grace expiry."""
    held, other = uuid.uuid4(), uuid.uuid4()
    await _seed_grace_tenant(held)
    await _seed_grace_tenant(other)

    async with tenant_session(held) as holder:
        assert await claim_sweep_lock(holder, sweeps.GRACE_EXPIRY, held) is True

        async with tenant_session(other) as s:
            assert await claim_sweep_lock(s, sweeps.GRACE_EXPIRY, other) is True
        async with tenant_session(held) as s:
            assert await claim_sweep_lock(s, sweeps.RETENTION, held) is True

        # ...but the same pair is genuinely taken.
        async with tenant_session(held) as s:
            assert await claim_sweep_lock(s, sweeps.GRACE_EXPIRY, held) is False


async def test_the_per_tenant_entrypoints_claim_before_doing_any_work() -> None:
    """``_per_tenant`` drives close_purge, dunning, usage_reconcile, the reaper, the
    deferred drain and the health sweep — all of them behind one claim."""
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
                state=AccountState.unverified,
                email_verified=False,
                created_at=NOW - timedelta(days=30),
            )
        )

    async with tenant_session(admin_id) as holder:
        assert await claim_sweep_lock(holder, sweeps.UNVERIFIED_REAP, admin_id) is True
        assert await sweeps.run_unverified_reap(now=NOW) == []

    assert await sweeps.run_unverified_reap(now=NOW) == [str(admin_id)]
