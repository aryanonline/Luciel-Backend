"""An unbound PAYG price is loud, and never voids blocks (round 7 WP-1 #4, §3.4.1b).

``ensure_metered_subscription`` returns None when ``STRIPE_PAYG_PRICE_ID`` is empty.
``report_usage`` then returned 0 with no log while ``accrued_blocks`` had already been
advanced, and the next period roll zeroed blocks that were never posted — a live-card
tenant's usage vanished behind a clean zero. Now, in a non-mock Stripe posture with a
live card: an ERROR line, one ``billing_meter_unbound`` audit event per (tenant,
accrual period), the watermarks untouched, and the roll refuses to zero an unposted
residual until a subscription exists to post it to.

Mock posture (CI / local) bills nothing by design and stays silent — asserted too.
FakeStripeGateway only; no network.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime, time

import pytest
from app.billing import metering
from app.billing.stripe_gateway import FakeStripeGateway, set_stripe_gateway
from app.config import get_settings
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.budget import ConversationBudget
from app.models.instance import Account, AccountState, Instance, LucielState
from app.runtime.budget import current_billing_period_start
from sqlalchemy import select

PERIOD_START = datetime.combine(current_billing_period_start(), time.min, tzinfo=UTC)


def _customer(admin: uuid.UUID) -> str:
    return f"cus_fake_{admin.hex[:16]}"


async def _seed(
    admin: uuid.UUID,
    *,
    counted: int,
    period_start: datetime = PERIOD_START,
    counted_period: date | None = None,
) -> uuid.UUID:
    """A payg_enabled tenant with a Stripe customer and NO subscription yet."""
    iid = uuid.uuid4()
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin}@x.com",
                email_lower=f"{admin}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="L",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )
        if counted:
            s.add(
                ConversationBudget(
                    id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    billing_period_start=counted_period or period_start.date(),
                    counted_conversations=counted,
                    counted_session_ids=[],
                )
            )
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        acct.billing_state = "payg_enabled"
        acct.stripe_customer_id = _customer(admin)
        acct.stripe_subscription_id = None
        acct.period_start = period_start
        await s.flush()
    return iid


async def _report(admin: uuid.UUID, iid: uuid.UUID, now: datetime | None = None) -> int:
    async with tenant_session(admin) as s:
        return await metering.report_usage(s, admin_id=admin, instance_id=iid, now=now)


async def _alarms(admin: uuid.UUID) -> list[dict[str, object]]:
    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog)
                    .where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "billing_meter_unbound",
                    )
                    .order_by(AdminAuditLog.seq.asc())
                )
            )
            .scalars()
            .all()
        )
    return [dict(r.event_payload) for r in rows]


async def _watermarks(admin: uuid.UUID) -> tuple[date | None, int, int, str | None]:
    async with tenant_session(admin) as s:
        a = await metering.get_or_create_billing_account(s, admin_id=admin)
        return (
            a.accrual_period_start,
            a.accrued_blocks,
            a.last_reported_blocks,
            a.stripe_subscription_id,
        )


def _errors(caplog) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records if r.name == "app.billing.metering" and r.levelno == logging.ERROR
    ]


@pytest.fixture
def real_posture_no_price(monkeypatch) -> FakeStripeGateway:
    """The deployment gap: a real Stripe binding (posture "test") with no metered price.
    The fake is injected so no RealStripeGateway is ever constructed."""
    monkeypatch.setattr(get_settings(), "stripe_integration_posture", "test")
    monkeypatch.setattr(get_settings(), "stripe_payg_price_id", "")
    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    yield gw
    set_stripe_gateway(None)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_unbound_price_with_a_live_card_is_loud_and_keeps_the_blocks(
    real_posture_no_price: FakeStripeGateway, caplog
) -> None:
    admin = uuid.uuid4()
    iid = await _seed(admin, counted=151)  # two blocks past the free 50
    with caplog.at_level(logging.ERROR, logger="app.billing.metering"):
        assert await _report(admin, iid) == 0
        assert await _report(admin, iid) == 0  # every turn is a retry — and an alarm

    assert real_posture_no_price.meter_events == []
    errors = _errors(caplog)
    assert len(errors) == 2
    assert "stripe_payg_price_id is empty" in errors[0].getMessage()
    assert str(admin) in errors[0].getMessage()
    # The blocks are still owed: the watermark was advanced and is NOT zeroed.
    assert await _watermarks(admin) == (PERIOD_START.date(), 2, 0, None)
    # One audit record per (tenant, period), not one per turn.
    (alarm,) = await _alarms(admin)
    assert alarm == {
        "period_start": PERIOD_START.date().isoformat(),
        "unreported_blocks": 2,
        "binding": "stripe_payg_price:unset",
    }

    # The reconcile sweep's backstop path finds the same residual and says so too,
    # without a second audit row for the same period.
    async with tenant_session(admin) as s:
        assert await metering.report_accrued_usage(s, admin_id=admin) == 0
    assert len(_errors(caplog)) == 3
    assert len(await _alarms(admin)) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_mock_posture_bills_nothing_by_design_and_stays_silent(monkeypatch, caplog) -> None:
    monkeypatch.setattr(get_settings(), "stripe_integration_posture", "mock")
    monkeypatch.setattr(get_settings(), "stripe_payg_price_id", "")
    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    try:
        admin = uuid.uuid4()
        iid = await _seed(admin, counted=151)
        with caplog.at_level(logging.ERROR, logger="app.billing.metering"):
            assert await _report(admin, iid) == 0
    finally:
        set_stripe_gateway(None)
    assert gw.meter_events == []
    assert _errors(caplog) == []
    assert await _alarms(admin) == []
    # Still never zeroed — the blocks are the truth of what ran.
    assert await _watermarks(admin) == (PERIOD_START.date(), 2, 0, None)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_period_roll_never_zeroes_blocks_that_were_never_posted(
    real_posture_no_price: FakeStripeGateway, monkeypatch
) -> None:
    """January accrued two blocks with no price bound. February arrives: the roll used to
    zero January's watermark (blocks voided) and start February fresh. Now January's
    residual stays put — nothing accrues for February until it is settled — and the
    moment a price is bound, January posts under January's key, then February's block
    under February's, so the meter sums to exactly what ran."""
    gw = real_posture_no_price
    admin = uuid.uuid4()
    january = datetime(2030, 1, 15, tzinfo=UTC)
    iid = await _seed(admin, counted=151, period_start=january)
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        acct.period_resets_at = datetime(2030, 2, 15, tzinfo=UTC)
        acct.accrual_period_start = january.date()
        acct.accrued_blocks = 2
        acct.last_reported_blocks = 0  # both January blocks unposted: no price, no sub
        await s.flush()
    # February: 60 conversations so far → one new block.
    async with tenant_session(admin) as s:
        s.add(
            ConversationBudget(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                billing_period_start=date(2030, 2, 15),
                counted_conversations=60,
                counted_session_ids=[],
            )
        )
    february_now = datetime(2030, 2, 20, tzinfo=UTC)

    assert await _report(admin, iid, now=february_now) == 0
    # NOT rolled, NOT zeroed: January's two blocks are still on the watermark.
    assert await _watermarks(admin) == (date(2030, 1, 15), 2, 0, None)
    assert gw.meter_events == []
    (alarm,) = await _alarms(admin)
    assert alarm["period_start"] == "2030-01-15" and alarm["unreported_blocks"] == 2

    # The operator binds the price. Stripe anchors the new subscription at the period
    # the counter already rolled to, so the counter is not re-keyed.
    monkeypatch.setattr(get_settings(), "stripe_payg_price_id", "price_bound_now")
    gw.now = datetime(2030, 2, 15, tzinfo=UTC)
    assert await _report(admin, iid, now=february_now) == 1
    cus = _customer(admin)
    assert [(e.identifier, e.value) for e in gw.meter_events] == [
        (f"usage:{cus}:2030-01-15:0-2", 2),  # January's residual, under January's key
        (f"usage:{cus}:2030-02-15:0-1", 1),  # February's block, under February's key
    ]
    assert gw.billed_quantity(cus) == 3
    period, accrued, reported, sub = await _watermarks(admin)
    assert (period, accrued, reported) == (date(2030, 2, 15), 1, 1)
    assert sub is not None
    assert gw.subscription_price(sub) == "price_bound_now"
