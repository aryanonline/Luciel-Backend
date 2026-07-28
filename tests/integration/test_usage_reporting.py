"""PAYG usage actually reaches Stripe (Vision §7, Arch §3.4.1b).

The metering math was already correct but nothing called the gateway, so overage past
the free 50 was computed and never billed. These tests drive the runtime-side entry
point (``report_usage_after_count``, called right after the fire-once conversation
counter increments) and assert usage records land at the block boundaries — and, just
as importantly, that they do NOT land anywhere else.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from app.billing import metering
from app.billing.stripe_gateway import FakeStripeGateway, set_stripe_gateway
from app.config import get_settings
from app.db.session import tenant_session
from app.models.budget import ConversationBudget
from app.models.instance import Account, AccountState, Instance, LucielState
from app.runtime.budget import current_billing_period_start

pytestmark = [pytest.mark.asyncio]


PAYG_PRICE = "price_test_payg_block"


@pytest.fixture
def gateway(monkeypatch) -> FakeStripeGateway:
    """Install the fake as the process gateway so the runtime entry point — which
    resolves the gateway itself rather than taking one — is exercised as written.

    The metered price is bound here because the fake gates on it exactly as the real
    gateway does: an unbound price means no subscription and therefore no usage at all
    (§3.4.1b). These tests are about the metering math for a PAYG-configured platform, so
    they need that configuration; the unbound case is asserted separately below.
    """
    monkeypatch.setattr(get_settings(), "stripe_payg_price_id", PAYG_PRICE)
    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    yield gw
    set_stripe_gateway(None)


async def _seed(
    admin: uuid.UUID,
    *,
    counted: int,
    billing_state: str = "payg_enabled",
    with_customer: bool = True,
    subscription_id: str | None = None,
) -> uuid.UUID:
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
                    billing_period_start=current_billing_period_start(),
                    counted_conversations=counted,
                    counted_session_ids=[str(uuid.uuid4()) for _ in range(counted)],
                )
            )
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        acct.billing_state = billing_state
        acct.stripe_customer_id = f"cus_fake_{admin.hex[:16]}" if with_customer else None
        acct.stripe_subscription_id = subscription_id
        acct.period_start = datetime(2026, 7, 1, tzinfo=UTC)
        await s.flush()
    return iid


async def _report(admin: uuid.UUID, iid: uuid.UUID) -> int:
    async with tenant_session(admin) as s:
        return await metering.report_usage_after_count(s, admin_id=admin, instance_id=iid)


# --- the thresholds -------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_nothing_reported_inside_the_free_fifty(gateway: FakeStripeGateway) -> None:
    admin = uuid.uuid4()
    iid = await _seed(admin, counted=50)
    assert await _report(admin, iid) == 0
    assert gateway.usage_records == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_conversation_fifty_one_reports_the_first_block(
    gateway: FakeStripeGateway,
) -> None:
    admin = uuid.uuid4()
    iid = await _seed(admin, counted=51, subscription_id="sub_test")
    assert await _report(admin, iid) == 1
    assert len(gateway.usage_records) == 1
    rec = gateway.usage_records[0]
    assert rec.subscription_id == "sub_test"
    assert rec.quantity == 1  # ONE 100-block, not one conversation and not dollars


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_nothing_reported_partway_through_a_block(gateway: FakeStripeGateway) -> None:
    """The block is already paid for; re-reporting every conversation would be a
    pointless Stripe call per turn."""
    admin = uuid.uuid4()
    iid = await _seed(admin, counted=52, subscription_id="sub_test")
    assert await _report(admin, iid) == 0
    assert gateway.usage_records == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_conversation_one_fifty_one_reports_the_second_block(
    gateway: FakeStripeGateway,
) -> None:
    admin = uuid.uuid4()
    iid = await _seed(admin, counted=151, subscription_id="sub_test")
    assert await _report(admin, iid) == 2
    assert [r.quantity for r in gateway.usage_records] == [2]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_retry_at_the_same_boundary_never_double_bills(
    gateway: FakeStripeGateway,
) -> None:
    """HARD CONSTRAINT #3: the (period, quantity) key makes a retried report a no-op,
    which is what lets the runtime swallow a Stripe failure and recover later."""
    admin = uuid.uuid4()
    iid = await _seed(admin, counted=51, subscription_id="sub_test")
    assert await _report(admin, iid) == 1
    assert await _report(admin, iid) == 1  # same quantity reported back to the caller
    assert len(gateway.usage_records) == 1  # but only ONE accepted record


# --- who is billable ------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_free_cap_tenant_is_never_metered(gateway: FakeStripeGateway) -> None:
    """A cardless tenant is stopped at the cap instead of billed (Vision §7), so there
    is nothing to report even far above 50."""
    admin = uuid.uuid4()
    iid = await _seed(admin, counted=251, billing_state="free_cap")
    assert await _report(admin, iid) == 0
    assert gateway.usage_records == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_no_stripe_customer_reports_nothing(gateway: FakeStripeGateway) -> None:
    admin = uuid.uuid4()
    iid = await _seed(admin, counted=51, with_customer=False)
    assert await _report(admin, iid) == 0
    assert gateway.usage_records == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_metered_subscription_is_bound_lazily_at_the_first_block(
    gateway: FakeStripeGateway,
) -> None:
    """``stripe_subscription_id`` was never written by anything, so reporting used to
    early-return forever. It is bound on first billable block — a tenant who never
    exceeds the free 50 still gets no subscription."""
    admin = uuid.uuid4()
    iid = await _seed(admin, counted=51, subscription_id=None)
    assert await _report(admin, iid) == 1
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        assert acct.stripe_subscription_id == f"sub_fake_{admin.hex[:16]}"
    assert gateway.usage_records[0].subscription_id == f"sub_fake_{admin.hex[:16]}"


# --- the metered price binding --------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_configured_price_reaches_the_usage_record(
    gateway: FakeStripeGateway,
) -> None:
    """The operator-supplied metered price is what the usage lands against. Nothing else
    proves the configured id is actually consumed rather than merely validated — a report
    against the wrong price is a wrong invoice, not an error."""
    admin = uuid.uuid4()
    iid = await _seed(admin, counted=51, subscription_id=None)
    assert await _report(admin, iid) == 1
    assert gateway.usage_records[0].price_id == PAYG_PRICE


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_no_configured_price_reports_nothing(monkeypatch) -> None:
    """The unconfigured default (§3.4.1b): with no metered price the gateway binds no
    subscription, so overage is NOT billed at a guessed price — it is not billed at all.
    This is the deploy-step failure mode, so it is asserted through the setting itself
    rather than through a stubbed gateway."""
    monkeypatch.setattr(get_settings(), "stripe_payg_price_id", "")
    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    try:
        admin = uuid.uuid4()
        iid = await _seed(admin, counted=51, subscription_id=None)
        assert await _report(admin, iid) == 0
        assert gw.usage_records == []
        async with tenant_session(admin) as s:
            acct = await metering.get_or_create_billing_account(s, admin_id=admin)
            assert acct.stripe_subscription_id is None
    finally:
        set_stripe_gateway(None)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_unbound_metered_price_reports_nothing_rather_than_guessing() -> None:
    """A gateway with no metered price returns None from ensure_metered_subscription;
    reporting must then stay silent — a charge at a guessed price is worse than no
    charge."""

    class NoPriceGateway(FakeStripeGateway):
        async def ensure_metered_subscription(self, *, customer_id: str) -> str | None:
            return None

    gw = NoPriceGateway()
    set_stripe_gateway(gw)
    try:
        admin = uuid.uuid4()
        iid = await _seed(admin, counted=51, subscription_id=None)
        assert await _report(admin, iid) == 0
        assert gw.usage_records == []
    finally:
        set_stripe_gateway(None)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_stripe_failure_never_breaks_the_lead_s_turn() -> None:
    """Reporting happens inline on the conversation path, so a gateway outage must
    degrade to "bill it at the next boundary", not to a failed turn."""

    class BrokenGateway(FakeStripeGateway):
        async def report_metered_usage(
            self, *, subscription_id: str, quantity: int, idempotency_key: str
        ) -> bool:
            raise RuntimeError("stripe down")

    set_stripe_gateway(BrokenGateway())
    try:
        admin = uuid.uuid4()
        iid = await _seed(admin, counted=51, subscription_id="sub_test")
        assert await _report(admin, iid) == 0  # swallowed, not raised
    finally:
        set_stripe_gateway(None)
