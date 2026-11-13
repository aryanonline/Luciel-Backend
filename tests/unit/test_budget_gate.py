"""Conversation budget gate — fire-once counter + at-cap (Architecture §3.4.1b)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime

import pytest
from app.db.session import tenant_session
from app.models.billing import BillingAccount
from app.models.budget import ConversationBudget
from app.runtime import budget
from sqlalchemy import select


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_count_session_once_is_idempotent_per_session():
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        first = await budget.count_session_once(s, admin_id=admin, instance_id=iid, session_id=sid)
        second = await budget.count_session_once(s, admin_id=admin, instance_id=iid, session_id=sid)
        assert first is True  # this call performed the increment
        assert second is False  # already counted — no double count
        state = await budget.check_budget(s, admin_id=admin, instance_id=iid)
        assert state.counted == 1  # exactly one (§3.4.1b counter precision)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_distinct_sessions_each_count_once():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        for _ in range(3):
            await budget.count_session_once(
                s, admin_id=admin, instance_id=iid, session_id=uuid.uuid4()
            )
        state = await budget.check_budget(s, admin_id=admin, instance_id=iid)
        assert state.counted == 3


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_at_cap_when_free_allowance_consumed_and_no_payment_method():
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        # Consume the full free allowance.
        state0 = await budget.check_budget(s, admin_id=admin, instance_id=iid)
        for _ in range(state0.free_allowance):
            await budget.count_session_once(
                s, admin_id=admin, instance_id=iid, session_id=uuid.uuid4()
            )
        # No payment method → at_cap (§3.4.1b).
        capped = await budget.check_budget(
            s, admin_id=admin, instance_id=iid, has_payment_method=False
        )
        assert capped.at_cap is True
        assert capped.remaining == 0
        # With a payment method, PAYG would continue (Arc 18) → not at_cap.
        with_pm = await budget.check_budget(
            s, admin_id=admin, instance_id=iid, has_payment_method=True
        )
        assert with_pm.at_cap is False


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_counted_session_is_never_at_cap_but_a_new_one_is():
    """Legal §A3 #3 / round 7 WP-1 #1: the cap gates NEW conversations. A session
    already in the fire-once ledger is one the tenant was counted for, so the gate
    keeps serving it after the counter reaches the free allowance — the 50th free
    conversation used to be cut after the very turn that counted it."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    in_flight = uuid.uuid4()
    async with tenant_session(admin) as s:
        state0 = await budget.check_budget(s, admin_id=admin, instance_id=iid)
        for _ in range(state0.free_allowance - 1):
            await budget.count_session_once(
                s, admin_id=admin, instance_id=iid, session_id=uuid.uuid4()
            )
        # The 50th conversation counts itself on its first served turn.
        assert await budget.count_session_once(
            s, admin_id=admin, instance_id=iid, session_id=in_flight
        )
        assert (await budget.check_budget(s, admin_id=admin, instance_id=iid)).at_cap is True

        # Its next turn names the session and is still served.
        continuing = await budget.check_budget(
            s, admin_id=admin, instance_id=iid, session_id=in_flight
        )
        assert continuing.at_cap is False
        assert continuing.counted == state0.free_allowance  # the read mutates nothing
        # A conversation the ledger has never seen is what the cap is for.
        fresh = await budget.check_budget(
            s, admin_id=admin, instance_id=iid, session_id=uuid.uuid4()
        )
        assert fresh.at_cap is True
        # A live card lifts the cap for everyone, counted or not.
        assert (
            await budget.check_budget(
                s, admin_id=admin, instance_id=iid, has_payment_method=True, session_id=uuid.uuid4()
            )
        ).at_cap is False


# --- the period anchor (P0-4) ---------------------------------------------- #


async def _account_billed_on(admin: uuid.UUID, day: datetime) -> None:
    from app.billing.metering import get_or_create_billing_account

    async with tenant_session(admin) as s:
        acct = await get_or_create_billing_account(s, admin_id=admin)
        acct.period_start = day
        await s.flush()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_counter_is_keyed_on_the_stripe_period_not_the_calendar_month():
    """A tenant Stripe bills on the 15th counts against a period starting the 15th.

    Keying on the calendar month instead zeroed their counter on the 1st — a second free
    50 every month, mid-Stripe-period.
    """
    admin, iid = uuid.uuid4(), uuid.uuid4()
    await _account_billed_on(admin, datetime(2026, 5, 15, tzinfo=UTC))

    async with tenant_session(admin) as s:
        await budget.count_session_once(s, admin_id=admin, instance_id=iid, session_id=uuid.uuid4())
        row = (
            await s.execute(select(ConversationBudget).where(ConversationBudget.admin_id == admin))
        ).scalar_one()
        # The 15th-anchored cycle that contains TODAY (the anchor rolls forward by
        # whole cycles when no invoice.paid moved it — 2026-09-05 audit F030), never
        # the calendar 1st.
        assert row.billing_period_start.day == 15
        assert (
            row.billing_period_start
            <= date.today()
            < row.billing_period_start.replace(
                year=row.billing_period_start.year + (row.billing_period_start.month == 12),
                month=row.billing_period_start.month % 12 + 1,
            )
        )
        assert row.billing_period_start != budget.current_billing_period_start()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_calendar_rolling_over_does_not_grant_a_second_free_fifty():
    """Same Stripe period, different calendar month: the count carries, so the tenant
    stays at cap instead of being handed a fresh allowance."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    await _account_billed_on(admin, datetime(2026, 5, 15, tzinfo=UTC))

    async with tenant_session(admin) as s:
        for _ in range(50):
            await budget.count_session_once(
                s, admin_id=admin, instance_id=iid, session_id=uuid.uuid4()
            )
    # A later calendar month (today is not May 2026) must read back the same period.
    async with tenant_session(admin) as s:
        state = await budget.check_budget(s, admin_id=admin, instance_id=iid)
    assert state.counted == 50
    assert state.at_cap is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_tenant_stripe_has_never_invoiced_falls_back_to_the_calendar_month():
    """No billing account means no Stripe period to anchor on; the calendar month is the
    only stable key available and the gate must not conjure an account to find one."""
    admin, iid = uuid.uuid4(), uuid.uuid4()
    async with tenant_session(admin) as s:
        await budget.count_session_once(s, admin_id=admin, instance_id=iid, session_id=uuid.uuid4())
        row = (
            await s.execute(select(ConversationBudget).where(ConversationBudget.admin_id == admin))
        ).scalar_one()
        assert row.billing_period_start == budget.current_billing_period_start()
        accounts = (
            (await s.execute(select(BillingAccount).where(BillingAccount.admin_id == admin)))
            .scalars()
            .all()
        )
        assert accounts == []  # the gate reads, it does not create


# --- concurrency (P0-5) ----------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_concurrent_turns_on_one_session_count_it_exactly_once():
    """Two turns of the same session racing must not bill the conversation twice.

    The read-modify-write both saw the session id absent and both incremented; the
    losing JSONB write also clobbered the winner's, so the id could go missing and the
    session be counted a third time later. Separate transactions, so the guard has to be
    the unique constraint rather than anything held in one session's identity map.
    """
    admin, iid, sid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def count() -> bool:
        async with tenant_session(admin) as s:
            return await budget.count_session_once(
                s, admin_id=admin, instance_id=iid, session_id=sid
            )

    results = await asyncio.gather(*(count() for _ in range(5)), return_exceptions=True)
    assert [r for r in results if isinstance(r, BaseException)] == []
    assert sum(1 for r in results if r is True) == 1  # exactly one call counted

    async with tenant_session(admin) as s:
        state = await budget.check_budget(s, admin_id=admin, instance_id=iid)
    assert state.counted == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_concurrent_distinct_sessions_lose_no_increment():
    """Ten sessions racing must total ten. Each used to read N and write N+1, so
    overlapping turns silently collapsed into a single increment — under-billing."""
    admin, iid = uuid.uuid4(), uuid.uuid4()

    async def count(sid: uuid.UUID) -> bool:
        async with tenant_session(admin) as s:
            return await budget.count_session_once(
                s, admin_id=admin, instance_id=iid, session_id=sid
            )

    sids = [uuid.uuid4() for _ in range(10)]
    results = await asyncio.gather(*(count(sid) for sid in sids), return_exceptions=True)
    assert [r for r in results if isinstance(r, BaseException)] == []

    async with tenant_session(admin) as s:
        state = await budget.check_budget(s, admin_id=admin, instance_id=iid)
    assert state.counted == 10


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_concurrent_first_conversations_do_not_500_on_the_budget_row():
    """Both first-conversations of a period used to INSERT the budget row and the loser
    violated uq_budget_period — an unhandled IntegrityError on a customer's message."""
    admin = uuid.uuid4()
    iid = uuid.uuid4()

    async def first_turn(sid: uuid.UUID) -> bool:
        async with tenant_session(admin) as s:
            return await budget.count_session_once(
                s, admin_id=admin, instance_id=iid, session_id=sid
            )

    results = await asyncio.gather(
        *(first_turn(uuid.uuid4()) for _ in range(4)), return_exceptions=True
    )
    assert [r for r in results if isinstance(r, BaseException)] == []

    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(ConversationBudget).where(ConversationBudget.admin_id == admin)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1  # one period row, not one per racing turn
