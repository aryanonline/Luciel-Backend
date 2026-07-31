"""Scheduled background sweeps (Legal §B5 / §A3).

The sweep LOGIC was already implemented and tested; what did not exist was anything
that called it — no scheduler, no discovery, so four compliance commitments never
ran. These tests cover the newly-added half against the real DB with RLS:

  * cross-tenant candidate discovery (``platform_sweep_candidates``, migration 0018)
    returns the right tenants per kind and NEVER leaks tenant data;
  * each job entrypoint reaches its per-tenant work through that discovery;
  * close-account purge and the day-7 dunning advance (both new per-tenant logic)
    honor their deadlines and are idempotent.

The cron expressions themselves are asserted in infra/test/sweep-schedules.test.ts.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.auth import reaper
from app.billing import dunning
from app.db.session import tenant_session
from app.jobs import sweeps
from app.lifecycle.close_purge import purge_closed_account_if_due
from app.models.audit import AdminAuditLog
from app.models.billing import BillingAccount
from app.models.connection import InstanceConnection
from app.models.conversation import Transcript
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


async def _seed_account(
    admin_id: uuid.UUID, state: AccountState, *, created_at: datetime | None = None
) -> None:
    async with tenant_session(admin_id) as s:
        s.add(
            Account(
                id=admin_id,
                admin_id=admin_id,
                user_id=admin_id,
                email=f"{admin_id}@x.com",
                email_lower=f"{admin_id}@x.com",
                password_hash="x",
                state=state,
                email_verified=state is not AccountState.unverified,
                **({} if created_at is None else {"created_at": created_at}),
            )
        )


async def _seed_instance(admin_id: uuid.UUID, state: LucielState, *, grace_start=None) -> uuid.UUID:
    iid = uuid.uuid4()
    async with tenant_session(admin_id) as s:
        s.add(
            Instance(
                id=iid,
                admin_id=admin_id,
                name="L",
                state=state,
                grace_window_started_at=grace_start,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
    return iid


async def _emit_closure(admin_id: uuid.UUID) -> datetime:
    """Write the closure audit row the purge reads its clock from, and return its
    timestamp. The audit log is append-only, so the row cannot be backdated — the
    tests move the sweep's ``now`` forward instead."""
    from app.audit import log as audit
    from app.models.audit import ActorType

    async with tenant_session(admin_id) as s:
        row = await audit.emit(
            s,
            admin_id=admin_id,
            actor_type=ActorType.admin_user,
            event_type="account_closure_initiated",
        )
        return row.created_at


async def test_discovery_selects_only_the_right_tenants_per_kind() -> None:
    live, in_grace, closed = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed_account(live, AccountState.verified)
    await _seed_instance(live, LucielState.active)
    await _seed_account(in_grace, AccountState.verified)
    await _seed_instance(in_grace, LucielState.luciel_grace_window, grace_start=NOW)
    await _seed_account(closed, AccountState.closed)

    assert live in await sweeps.sweep_candidates(sweeps.RETENTION)
    assert in_grace not in await sweeps.sweep_candidates(sweeps.RETENTION)
    assert await sweeps.sweep_candidates(sweeps.GRACE_EXPIRY) == [in_grace]
    assert closed in await sweeps.sweep_candidates(sweeps.CLOSE_PURGE)
    assert live not in await sweeps.sweep_candidates(sweeps.CLOSE_PURGE)


async def test_unknown_sweep_kind_discovers_nothing() -> None:
    """A typo'd schedule must sweep NOTHING rather than every tenant."""
    await _seed_account(uuid.uuid4(), AccountState.verified)
    assert await sweeps.sweep_candidates("not_a_sweep") == []


async def test_retention_job_prunes_aged_rows_through_discovery() -> None:
    admin_id = uuid.uuid4()
    await _seed_account(admin_id, AccountState.verified)
    iid = await _seed_instance(admin_id, LucielState.active)
    async with tenant_session(admin_id) as s:
        for age_days in (400, 10):
            s.add(
                Transcript(
                    id=uuid.uuid4(),
                    admin_id=admin_id,
                    instance_id=iid,
                    session_id=uuid.uuid4(),
                    role="lead",
                    text="hi",
                    created_at=NOW - timedelta(days=age_days),
                )
            )

    swept = await sweeps.run_retention_prune(now=NOW)
    assert swept[str(admin_id)]["transcript"] == 1

    async with tenant_session(admin_id) as s:
        remaining = (
            (await s.execute(select(Transcript).where(Transcript.admin_id == admin_id)))
            .scalars()
            .all()
        )
    assert len(remaining) == 1


async def test_grace_purge_job_fires_the_cascade_at_day_30() -> None:
    admin_id = uuid.uuid4()
    await _seed_account(admin_id, AccountState.verified)
    await _seed_instance(
        admin_id, LucielState.luciel_grace_window, grace_start=NOW - timedelta(days=31)
    )

    deleted = await sweeps.run_grace_purge(now=NOW)
    assert str(admin_id) in deleted

    async with tenant_session(admin_id) as s:
        inst = (
            await s.execute(select(Instance).where(Instance.admin_id == admin_id))
        ).scalar_one_or_none()
    assert inst is None


async def test_close_purge_waits_for_the_grace_window_then_releases_the_email() -> None:
    admin_id = uuid.uuid4()
    await _seed_account(admin_id, AccountState.closed)
    closed_at = await _emit_closure(admin_id)

    # Inside the 30-day window: the shell (email + password hash) must survive.
    async with tenant_session(admin_id) as s:
        assert (
            await purge_closed_account_if_due(
                s, admin_id=admin_id, now=closed_at + timedelta(days=5)
            )
            is False
        )
    async with tenant_session(admin_id) as s:
        assert (
            await s.execute(select(Account).where(Account.admin_id == admin_id))
        ).scalar_one_or_none() is not None

    # Past day 30: purged, and the purge is recorded as a lifecycle transition.
    later = closed_at + timedelta(days=31)
    assert await sweeps.run_close_account_purge(now=later) == [str(admin_id)]
    async with tenant_session(admin_id) as s:
        assert (
            await s.execute(select(Account).where(Account.admin_id == admin_id))
        ).scalar_one_or_none() is None
        events = (
            (
                await s.execute(
                    select(AdminAuditLog.event_payload).where(
                        AdminAuditLog.admin_id == admin_id,
                        AdminAuditLog.event_type == "lifecycle_transition",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert any(e.get("to") == "account_purged" for e in events)

    # Idempotent: the schedule delivers at-least-once.
    assert await sweeps.run_close_account_purge(now=later) == []


async def test_close_purge_refuses_when_no_closure_event_exists() -> None:
    """`closed` with no closure audit row is a data anomaly — never destroy on a guess."""
    admin_id = uuid.uuid4()
    await _seed_account(admin_id, AccountState.closed)
    async with tenant_session(admin_id) as s:
        assert (
            await purge_closed_account_if_due(s, admin_id=admin_id, now=NOW + timedelta(days=90))
            is False
        )


async def test_dunning_job_advances_day_seven_and_sends_action_tools_dormant() -> None:
    admin_id = uuid.uuid4()
    await _seed_account(admin_id, AccountState.verified)
    iid = await _seed_instance(admin_id, LucielState.active)
    async with tenant_session(admin_id) as s:
        s.add(
            BillingAccount(
                admin_id=admin_id,
                billing_state="payg_enabled",
                dunning_state=dunning.RETRYING,
                dunning_started_at=NOW - timedelta(days=8),
            )
        )
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin_id,
                instance_id=iid,
                connection_type="calendar",
                auth_class="oauth2",
                provider="google",
                status="connected",
            )
        )

    assert await sweeps.run_dunning_transitions(now=NOW) == [str(admin_id)]

    async with tenant_session(admin_id) as s:
        acct = (
            await s.execute(select(BillingAccount).where(BillingAccount.admin_id == admin_id))
        ).scalar_one()
        assert acct.dunning_state == dunning.REDUCED
        conn = (
            await s.execute(
                select(InstanceConnection).where(InstanceConnection.admin_id == admin_id)
            )
        ).scalar_one()
        assert conn.status == "dormant"

    # Idempotent: already reduced, nothing further to advance.
    assert await sweeps.run_dunning_transitions(now=NOW) == []


async def test_unverified_reap_discovery_finds_only_unverified_accounts() -> None:
    unverified, verified = uuid.uuid4(), uuid.uuid4()
    await _seed_account(unverified, AccountState.unverified)
    await _seed_account(verified, AccountState.verified)

    candidates = await sweeps.sweep_candidates(sweeps.UNVERIFIED_REAP)
    assert unverified in candidates
    assert verified not in candidates


async def test_unverified_reap_purges_aged_accounts_and_leaves_the_rest() -> None:
    """§3.7.1a / Legal §A1: unverified for 7 days means the row is gone — which is also
    what releases the email address for signup. The reaper had no caller at all, so the
    promise never ran; these assertions are on the newly-wired sweep, not the primitive.
    """
    aged, fresh, verified = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _seed_account(aged, AccountState.unverified, created_at=NOW - timedelta(days=8))
    await _seed_account(fresh, AccountState.unverified, created_at=NOW - timedelta(days=2))
    await _seed_account(verified, AccountState.verified, created_at=NOW - timedelta(days=400))

    assert await sweeps.run_unverified_reap(now=NOW) == [str(aged)]

    async def _exists(admin_id: uuid.UUID) -> bool:
        async with tenant_session(admin_id) as s:
            return (
                await s.execute(select(Account).where(Account.admin_id == admin_id))
            ).scalar_one_or_none() is not None

    assert not await _exists(aged), "the 7-day window expired and the row survived"
    assert await _exists(fresh), "an account still inside its window was destroyed"
    assert await _exists(verified), "a verified account was reaped by age alone"

    # The tombstone outlives the row it describes: the audit log is not cascade material.
    async with tenant_session(aged) as s:
        reaped = (
            (
                await s.execute(
                    select(AdminAuditLog.event_payload).where(
                        AdminAuditLog.admin_id == aged,
                        AdminAuditLog.event_type == "unverified_account_reaped",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(reaped) == 1
    assert reaped[0]["reason"] == "7_day_unverified_window_expired"

    # Idempotent: schedules deliver at-least-once.
    assert await sweeps.run_unverified_reap(now=NOW) == []


async def test_unverified_reap_respects_a_verification_that_lands_mid_sweep() -> None:
    """Discovery filters on state only — the age cutoff lives in config, so re-deriving
    it per tenant is what keeps the two halves from drifting. The same re-check is what
    makes a stale candidate list safe: verify between discovery and the purge and the
    account must survive."""
    admin_id = uuid.uuid4()
    await _seed_account(admin_id, AccountState.unverified, created_at=NOW - timedelta(days=30))

    async with tenant_session(admin_id) as s:
        acct = (await s.execute(select(Account).where(Account.admin_id == admin_id))).scalar_one()
        acct.state = AccountState.verified

    async with tenant_session(admin_id) as s:
        assert await reaper.reap_account_if_due(s, admin_id=admin_id, now=NOW) is False
    async with tenant_session(admin_id) as s:
        assert (
            await s.execute(select(Account).where(Account.admin_id == admin_id))
        ).scalar_one_or_none() is not None


async def test_dunning_job_leaves_a_tenant_inside_the_retry_window_alone() -> None:
    admin_id = uuid.uuid4()
    await _seed_account(admin_id, AccountState.verified)
    async with tenant_session(admin_id) as s:
        s.add(
            BillingAccount(
                admin_id=admin_id,
                billing_state="payg_enabled",
                dunning_state=dunning.RETRYING,
                dunning_started_at=NOW - timedelta(days=3),
            )
        )
    async with tenant_session(admin_id) as s:
        assert await dunning.advance_if_due(s, admin_id=admin_id, now=NOW) is None
