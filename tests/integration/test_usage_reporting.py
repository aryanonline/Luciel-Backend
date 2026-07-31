"""PAYG usage actually reaches Stripe (Vision §7, Arch §3.4.1b).

The metering math was already correct but nothing called the gateway, so overage past
the free 50 was computed and never billed. These tests drive the runtime-side entry
point (``report_usage_after_count``, called right after the fire-once conversation
counter increments) and assert usage records land at the block boundaries — and, just
as importantly, that they do NOT land anywhere else.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time

import pytest
from app.billing import metering
from app.billing.stripe_gateway import FakeStripeGateway, set_stripe_gateway
from app.config import get_settings
from app.db.session import tenant_session
from app.models.budget import ConversationBudget
from app.models.instance import Account, AccountState, Instance, LucielState
from app.runtime.budget import current_billing_period_start
from sqlalchemy import update

pytestmark = [pytest.mark.asyncio]


PAYG_PRICE = "price_test_payg_block"

# The counter is keyed on the account's Stripe period (P0-4), so the seed writes one
# anchor to both places; a hardcoded date here would silently seed an empty counter.
STRIPE_PERIOD_START = datetime.combine(current_billing_period_start(), time.min, tzinfo=UTC)


def _customer(admin: uuid.UUID) -> str:
    """The Stripe customer id the seed binds — meter events are keyed on it."""
    return f"cus_fake_{admin.hex[:16]}"


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
    period_start: datetime = STRIPE_PERIOD_START,
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
                    billing_period_start=period_start.date(),
                    counted_conversations=counted,
                    counted_session_ids=[str(uuid.uuid4()) for _ in range(counted)],
                )
            )
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        acct.billing_state = billing_state
        acct.stripe_customer_id = _customer(admin) if with_customer else None
        acct.stripe_subscription_id = subscription_id
        acct.period_start = period_start
        await s.flush()
    return iid


async def _report(admin: uuid.UUID, iid: uuid.UUID) -> int:
    async with tenant_session(admin) as s:
        return await metering.report_usage_after_count(s, admin_id=admin, instance_id=iid)


async def _set_counted(admin: uuid.UUID, iid: uuid.UUID, counted: int) -> None:
    async with tenant_session(admin) as s:
        await s.execute(
            update(ConversationBudget)
            .where(
                ConversationBudget.admin_id == admin,
                ConversationBudget.instance_id == iid,
            )
            .values(counted_conversations=counted)
        )


# --- the thresholds -------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_nothing_reported_inside_the_free_fifty(gateway: FakeStripeGateway) -> None:
    admin = uuid.uuid4()
    iid = await _seed(admin, counted=50)
    assert await _report(admin, iid) == 0
    assert gateway.meter_events == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_conversation_fifty_one_reports_the_first_block(
    gateway: FakeStripeGateway,
) -> None:
    admin = uuid.uuid4()
    iid = await _seed(admin, counted=51, subscription_id="sub_test")
    assert await _report(admin, iid) == 1
    assert len(gateway.meter_events) == 1
    ev = gateway.meter_events[0]
    # The meter is keyed on the CUSTOMER; the subscription only turns what the meter
    # accumulated into an invoice line.
    assert ev.customer_id == _customer(admin)
    assert ev.value == 1  # ONE 100-block, not one conversation and not dollars


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_nothing_reported_partway_through_an_already_reported_block(
    gateway: FakeStripeGateway,
) -> None:
    """Stripe already has these blocks; there is no delta to post, so the meter must not
    receive a zero-value event per turn. The watermark, not the boundary, suppresses it."""
    admin = uuid.uuid4()
    iid = await _seed(admin, counted=51, subscription_id="sub_test")
    assert await _report(admin, iid) == 1
    await _set_counted(admin, iid, 52)
    assert await _report(admin, iid) == 0
    assert [e.value for e in gateway.meter_events] == [1]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_conversation_one_fifty_one_reports_the_second_block(
    gateway: FakeStripeGateway,
) -> None:
    """Crossing straight to block 2 from an empty watermark is a single delta of 2 —
    the blocks accrued, not the block number reached."""
    admin = uuid.uuid4()
    iid = await _seed(admin, counted=151, subscription_id="sub_test")
    assert await _report(admin, iid) == 2
    assert [e.value for e in gateway.meter_events] == [2]
    assert gateway.billed_quantity(_customer(admin)) == 2


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_retry_at_the_same_boundary_never_double_bills(
    gateway: FakeStripeGateway,
) -> None:
    """HARD CONSTRAINT #3: a retried report must never bill twice. The watermark stops
    the second call before it reaches Stripe at all; the event identifier remains the
    backstop underneath it."""
    admin = uuid.uuid4()
    iid = await _seed(admin, counted=51, subscription_id="sub_test")
    assert await _report(admin, iid) == 1
    assert await _report(admin, iid) == 0  # nothing new accrued since the watermark
    assert len(gateway.meter_events) == 1  # ONE accepted event
    assert gateway.billed_quantity(_customer(admin)) == 1


# --- what Stripe actually invoices (P0-3) ---------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_successive_blocks_post_deltas_so_the_meter_sums_to_the_usage(
    gateway: FakeStripeGateway,
) -> None:
    """Meter events are ADDITIVE, so what is posted must be the delta past the watermark.

    Posting the period total instead would invoice 1+2+3 = 6 blocks for 3 blocks of
    usage — $234 for $117 — and the error compounds with usage, so the heaviest
    customers would be the most overbilled. Recording the calls alone cannot catch it:
    the per-call values are each defensible; only the sum is wrong.
    """
    admin = uuid.uuid4()
    iid = await _seed(admin, counted=51, subscription_id="sub_test")

    assert await _report(admin, iid) == 1
    await _set_counted(admin, iid, 151)
    assert await _report(admin, iid) == 1
    await _set_counted(admin, iid, 251)
    assert await _report(admin, iid) == 1

    assert [e.value for e in gateway.meter_events] == [1, 1, 1]
    # 3 blocks of usage == 3 blocks invoiced. Posting totals here would sum to 6.
    assert gateway.billed_quantity(_customer(admin)) == 3


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_delta_spanning_several_blocks_is_posted_once_at_its_true_size(
    gateway: FakeStripeGateway,
) -> None:
    """A tenant can cross several blocks between two reports (a quiet sweep window, a
    burst of traffic). The event must carry the whole span, and the identifier must name
    it, so the next report starts exactly where this one ended — no gap, no overlap."""
    admin = uuid.uuid4()
    iid = await _seed(admin, counted=51, subscription_id="sub_test")
    assert await _report(admin, iid) == 1

    await _set_counted(admin, iid, 401)  # blocks 2, 3 and 4 all at once
    assert await _report(admin, iid) == 3

    assert [e.value for e in gateway.meter_events] == [1, 3]
    assert [e.identifier.rsplit(":", 1)[-1] for e in gateway.meter_events] == ["0-1", "1-4"]
    assert gateway.billed_quantity(_customer(admin)) == 4  # ceil(350/100)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_retry_after_a_timeout_is_deduped_rather_than_added_twice(
    gateway: FakeStripeGateway,
) -> None:
    """The dangerous case for an ADDITIVE meter: Stripe accepted the event but the
    response never came back, so the watermark did not advance and the caller retries.

    The retry recomputes the same watermark span, so it carries the same identifier and
    Stripe drops it. Without that the value would be added a second time — a silent
    double-bill that no amount of retry-count tuning would fix.
    """

    class _LosesTheResponse:
        """Accepts into the meter, then fails as a timeout would — once."""

        def __init__(self, inner: FakeStripeGateway) -> None:
            self._inner = inner
            self._armed = True

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

        async def report_meter_event(self, **kwargs):
            accepted = await self._inner.report_meter_event(**kwargs)
            if self._armed:
                self._armed = False
                raise RuntimeError("timeout after Stripe accepted the event")
            return accepted

    admin = uuid.uuid4()
    iid = await _seed(admin, counted=151, subscription_id="sub_test")

    set_stripe_gateway(_LosesTheResponse(gateway))
    assert await _report(admin, iid) == 0  # swallowed; watermark unmoved
    assert [e.value for e in gateway.meter_events] == [2]  # but Stripe DID get it

    set_stripe_gateway(gateway)
    assert await _report(admin, iid) == 2  # the retry, same span
    # Two calls reached the meter, one value was counted: exactly-once per block.
    assert gateway.billed_quantity(_customer(admin)) == 2
    assert len(gateway.meter_events) == 1


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_mid_month_stripe_period_bills_the_counter_it_actually_counted(
    gateway: FakeStripeGateway,
) -> None:
    """The counter and the event identifier must name the SAME period (P0-4).

    They used to disagree: the counter was keyed on the calendar month while the key was
    keyed on the Stripe period. A tenant billed on the 15th therefore got their counter
    zeroed on the 1st — a second free 50 every month — and the block they crossed after
    the rollover reused an identifier already spent on the first block. Stripe deduped
    it, so that block was never invoiced at all.
    """
    anchor = datetime(2026, 5, 15, tzinfo=UTC)
    admin = uuid.uuid4()
    iid = await _seed(admin, counted=51, subscription_id="sub_test", period_start=anchor)

    assert await _report(admin, iid) == 1
    await _set_counted(admin, iid, 151)
    assert await _report(admin, iid) == 1

    assert [e.value for e in gateway.meter_events] == [1, 1]
    assert gateway.billed_quantity(_customer(admin)) == 2
    # The identifier names the Stripe anchor the counter is keyed on, not the calendar
    # month, and the watermark span rather than a bare quantity.
    cus = _customer(admin)
    assert [e.identifier for e in gateway.meter_events] == [
        f"usage:{cus}:2026-05-15:0-1",
        f"usage:{cus}:2026-05-15:1-2",
    ]


# --- who is billable ------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_free_cap_tenant_is_never_metered(gateway: FakeStripeGateway) -> None:
    """A cardless tenant is stopped at the cap instead of billed (Vision §7), so there
    is nothing to report even far above 50."""
    admin = uuid.uuid4()
    iid = await _seed(admin, counted=251, billing_state="free_cap")
    assert await _report(admin, iid) == 0
    assert gateway.meter_events == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_no_stripe_customer_reports_nothing(gateway: FakeStripeGateway) -> None:
    admin = uuid.uuid4()
    iid = await _seed(admin, counted=51, with_customer=False)
    assert await _report(admin, iid) == 0
    assert gateway.meter_events == []


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
    # The event names the customer; the subscription is what invoices what it accrued.
    assert gateway.meter_events[0].customer_id == _customer(admin)


# --- the metered price binding --------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_configured_price_backs_the_subscription_the_meter_invoices(
    gateway: FakeStripeGateway,
) -> None:
    """The operator-supplied meter-backed price is what the accrued usage invoices
    against. Nothing else proves the configured id is actually consumed rather than
    merely validated — a subscription on the wrong price is a wrong invoice, not an
    error. The price no longer rides on the usage event itself, so it is asserted where
    it now lives: on the subscription the gateway created."""
    admin = uuid.uuid4()
    iid = await _seed(admin, counted=51, subscription_id=None)
    assert await _report(admin, iid) == 1
    assert gateway.subscription_price(f"sub_fake_{admin.hex[:16]}") == PAYG_PRICE


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
        assert gw.meter_events == []
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
        assert gw.meter_events == []
    finally:
        set_stripe_gateway(None)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_stripe_failure_never_breaks_the_lead_s_turn() -> None:
    """Reporting happens inline on the conversation path, so a gateway outage must
    degrade to "bill it at the next boundary", not to a failed turn."""

    class BrokenGateway(FakeStripeGateway):
        async def report_meter_event(
            self, *, customer_id: str, value: int, identifier: str
        ) -> bool:
            raise RuntimeError("stripe down")

    set_stripe_gateway(BrokenGateway())
    try:
        admin = uuid.uuid4()
        iid = await _seed(admin, counted=51, subscription_id="sub_test")
        assert await _report(admin, iid) == 0  # swallowed, not raised
    finally:
        set_stripe_gateway(None)


# --- the watermark (P1-12 / P1-13) ------------------------------------------- #


class _FlakyGateway:
    """Wraps the fake and fails the first N posts, the way a Stripe blip would."""

    def __init__(self, inner: FakeStripeGateway, failures: int) -> None:
        self._inner = inner
        self._failures = failures

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    async def report_meter_event(self, **kwargs):
        if self._failures > 0:
            self._failures -= 1
            raise RuntimeError("stripe unavailable")
        return await self._inner.report_meter_event(**kwargs)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_stripe_blip_at_the_boundary_does_not_lose_the_block(
    gateway: FakeStripeGateway,
) -> None:
    """Reporting used to fire only on the exact boundary conversation and swallow the
    error, so a blip at conversation 151 lost block 2 for good — nothing recorded that a
    report was owed and nothing came back for it. The next turn must retry.
    """
    admin = uuid.uuid4()
    iid = await _seed(admin, counted=151, subscription_id="sub_test")

    set_stripe_gateway(_FlakyGateway(gateway, failures=1))
    assert await _report(admin, iid) == 0  # the blip, swallowed
    assert gateway.meter_events == []

    set_stripe_gateway(gateway)
    await _set_counted(admin, iid, 152)  # an ordinary turn, not a boundary
    assert await _report(admin, iid) == 2
    assert [e.value for e in gateway.meter_events] == [2]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_reconcile_sweep_bills_a_tenant_who_stopped_conversing(
    gateway: FakeStripeGateway,
) -> None:
    """The per-turn retry only helps if there is a next turn. A tenant who crosses a
    block, hits a blip, and goes quiet needs the sweep or the block is never billed."""
    from app.jobs import sweeps

    admin = uuid.uuid4()
    iid = await _seed(admin, counted=151, subscription_id="sub_test")

    set_stripe_gateway(_FlakyGateway(gateway, failures=1))
    assert await _report(admin, iid) == 0
    set_stripe_gateway(gateway)

    assert await sweeps.run_usage_reconcile() == [str(admin)]
    assert [e.value for e in gateway.meter_events] == [2]
    # Nothing left owing → the second run is a no-op, not a double-bill. On an additive
    # meter a repeat sweep would otherwise keep adding to the same period.
    assert await sweeps.run_usage_reconcile() == []
    assert gateway.billed_quantity(_customer(admin)) == 2


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_detaching_a_card_bills_the_overage_already_run(
    gateway: FakeStripeGateway,
) -> None:
    """build_budget reports zero blocks without a live card and report_usage refuses a
    non-payg account, so 250 conversations followed by a card detach voided two real
    blocks — a free-usage path anyone could take deliberately."""
    from app.billing import webhooks as billing_webhooks
    from app.billing.stripe_gateway import StripeEvent

    admin = uuid.uuid4()
    iid = await _seed(admin, counted=250, subscription_id="sub_test")
    async with tenant_session(admin) as s:
        await metering.report_usage(s, admin_id=admin, instance_id=iid, gateway=gateway)
    assert gateway.billed_quantity(_customer(admin)) == 2

    # They run one more block, then pull the card before the invoice.
    await _set_counted(admin, iid, 260)
    async with tenant_session(admin) as s:
        await metering.report_usage(s, admin_id=admin, instance_id=iid, gateway=gateway)
    async with tenant_session(admin) as s:
        await billing_webhooks.handle_event(
            s,
            admin_id=admin,
            event=StripeEvent(event_id="evt_detach_bill", type="payment_method.detached", data={}),
        )

    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        assert acct.billing_state == "free_cap"
        assert acct.accrued_blocks == 3  # ceil(210/100)
        assert acct.last_reported_blocks == 3
    assert gateway.billed_quantity(_customer(admin)) == 3  # not voided by the detach
    # The detach flush posts only the RESIDUAL block, not the period total again.
    assert [e.value for e in gateway.meter_events] == [2, 1]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_new_period_starts_owing_stripe_nothing(gateway: FakeStripeGateway) -> None:
    """The watermark is period-scoped like the counter: the invoice settled everything
    reported, so leaving it set would suppress the new period's first real block."""
    from app.billing import webhooks as billing_webhooks
    from app.billing.stripe_gateway import StripeEvent

    admin = uuid.uuid4()
    iid = await _seed(admin, counted=151, subscription_id="sub_test")
    assert await _report(admin, iid) == 2

    async with tenant_session(admin) as s:
        await billing_webhooks.handle_event(
            s,
            admin_id=admin,
            event=StripeEvent(event_id="evt_new_period", type="invoice.paid", data={}),
        )
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        assert (acct.accrued_blocks, acct.last_reported_blocks) == (0, 0)
