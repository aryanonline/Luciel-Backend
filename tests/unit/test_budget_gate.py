"""Conversation budget gate — fire-once counter + at-cap (Architecture §3.4.1b)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from app.db.session import tenant_session
from app.models.billing import BillingAccount
from app.models.budget import ConversationBudget
from app.runtime import budget
from sqlalchemy import select

pytestmark = [pytest.mark.asyncio]


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
        assert row.billing_period_start == date(2026, 5, 15)
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
