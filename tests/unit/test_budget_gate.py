"""Conversation budget gate — fire-once counter + at-cap (Architecture §3.4.1b)."""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.runtime import budget

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
