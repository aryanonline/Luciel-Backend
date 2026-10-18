"""Reopen at the edges: the day-30 boundary, billing, and a Luciel that is already gone
(round 7 WP-5 / owner decision D5).

``tests/contract/test_account_reopen.py`` drives the reopen through the API on an
ordinary day. These are the conditions an ordinary day does not produce:

* the reopen and the day-30 PURGE running at the same moment (two tasks, clocks a
  second apart) — the purge must not destroy an account its owner just got back;
* the same for the Luciel: its own restore against the grace-expiry sweep;
* BILLING after a reopen — closure ended the metered subscription with a final
  invoice; the next billable block must bind a new one and bill only what is new;
* a closure whose two clocks (account, Luciel grace) must end at the same instant.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, time, timedelta

import pytest
from app.billing import metering
from app.billing.stripe_gateway import FakeStripeGateway, set_stripe_gateway
from app.config import get_settings
from app.db.session import tenant_session
from app.lifecycle import cascade, close_purge, grace_expiry, reopen, state_machine
from app.lifecycle.state_machine import LifecycleError
from app.models.budget import ConversationBudget
from app.models.instance import Account, AccountState, Instance, LucielState
from app.runtime.budget import current_billing_period_start
from sqlalchemy import select, update

pytestmark = [pytest.mark.asyncio]

PAYG_PRICE = "price_test_payg_block"
STRIPE_PERIOD_START = datetime.combine(current_billing_period_start(), time.min, tzinfo=UTC)


async def _seed(*, luciel: LucielState | None = LucielState.active) -> tuple[uuid.UUID, uuid.UUID]:
    admin, iid = uuid.uuid4(), uuid.uuid4()
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
        if luciel is not None:
            s.add(
                Instance(
                    id=iid,
                    admin_id=admin,
                    name="L",
                    state=luciel,
                    embed_key_public_id=f"vm_live_{uuid.uuid4().hex[:10]}",
                    channels=[],
                    tools=[],
                    escalation={},
                    personality={},
                )
            )
    return admin, iid


async def _close(admin: uuid.UUID) -> datetime:
    async with tenant_session(admin) as s:
        await cascade.close_account(s, admin, admin)
    async with tenant_session(admin) as s:
        closed_at = (
            await s.execute(select(Account.closed_at).where(Account.id == admin))
        ).scalar_one()
    assert closed_at is not None
    return closed_at


async def _account(admin: uuid.UUID) -> Account | None:
    async with tenant_session(admin) as s:
        return (await s.execute(select(Account).where(Account.id == admin))).scalar_one_or_none()


async def _instance(admin: uuid.UUID) -> Instance | None:
    async with tenant_session(admin) as s:
        return (
            await s.execute(select(Instance).where(Instance.admin_id == admin))
        ).scalar_one_or_none()


# --------------------------------------------------------------------------- #
# The two clocks, and the window                                               #
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_accounts_window_and_the_luciels_grace_window_end_together():
    """One instant for both clocks. A grace window stamped even a second before
    ``closed_at`` would let the grace-expiry sweep destroy the Luciel while the reopen
    that promises to bring it back was still on offer."""
    admin, _ = await _seed()
    closed_at = await _close(admin)
    inst = await _instance(admin)
    assert inst is not None and inst.grace_window_started_at == closed_at
    deadline = close_purge.reopen_deadline(closed_at)
    assert grace_expiry.grace_window_deadline(closed_at) == deadline

    # One second before the deadline: the sweep leaves the Luciel, the reopen works.
    async with tenant_session(admin) as s:
        assert (
            await grace_expiry.expire_grace_window_if_due(
                s, admin_id=admin, now=deadline - timedelta(seconds=1)
            )
            is None
        )
    async with tenant_session(admin) as s:
        outcome = await reopen.reopen_account(s, admin, admin, now=deadline - timedelta(seconds=1))
    assert (outcome.luciel_restored, outcome.luciel_state) == (True, "active")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_reopen_refuses_at_the_deadline_and_on_an_open_account():
    admin, _ = await _seed()
    async with tenant_session(admin) as s:
        with pytest.raises(LifecycleError, match="account_not_closed"):
            await reopen.reopen_account(s, admin, admin)

    closed_at = await _close(admin)
    async with tenant_session(admin) as s:
        with pytest.raises(LifecycleError, match="reopen_window_passed"):
            await reopen.reopen_account(s, admin, admin, now=close_purge.reopen_deadline(closed_at))
    acct = await _account(admin)
    assert acct is not None and acct.state == AccountState.closed


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_reopen_says_so_when_the_luciel_is_already_gone():
    """The closure deleted the Luciel, and the cascade has since destroyed it (an
    operator-run expiry, or the sliver at the boundary). The account still reopens —
    honestly: nothing was restored, and the outcome and the audit row say that."""
    admin, _ = await _seed()
    await _close(admin)
    async with tenant_session(admin) as s:
        await cascade.hard_delete_cascade(s, admin)

    async with tenant_session(admin) as s:
        outcome = await reopen.reopen_account(s, admin, admin)
    assert (outcome.luciel_restored, outcome.luciel_state) == (False, None)
    acct = await _account(admin)
    assert acct is not None and acct.state == AccountState.verified


# --------------------------------------------------------------------------- #
# The day-30 boundary, two tasks                                               #
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_reopen_in_flight_makes_the_purge_wait_and_then_do_nothing(monkeypatch):
    """The owner reopens with a second to spare; the purge sweep, on a task whose clock
    reads a second later, starts while that transaction is still open. Unserialized,
    the sweep read ``closed``, judged the window over, and destroyed the account the
    moment the reopen committed."""
    admin, _ = await _seed()
    closed_at = await _close(admin)
    deadline = close_purge.reopen_deadline(closed_at)

    holding, release = asyncio.Event(), asyncio.Event()
    real_record = cascade.closure_record

    async def _slow_record(session, admin_id):
        holding.set()  # the account row is locked by now
        await release.wait()
        return await real_record(session, admin_id)

    monkeypatch.setattr(reopen.cascade, "closure_record", _slow_record)

    async def _reopen() -> reopen.ReopenOutcome:
        async with tenant_session(admin) as s:
            return await reopen.reopen_account(s, admin, admin, now=deadline - timedelta(seconds=1))

    async def _purge() -> bool:
        async with tenant_session(admin) as s:
            return await close_purge.purge_closed_account_if_due(
                s, admin_id=admin, now=deadline + timedelta(seconds=1)
            )

    reopen_task = asyncio.create_task(_reopen())
    await asyncio.wait_for(holding.wait(), timeout=20)
    purge_task = asyncio.create_task(_purge())
    done, _ = await asyncio.wait({purge_task}, timeout=1.5)
    try:
        assert not done, "the purge judged the account without waiting for the reopen"
    finally:
        release.set()
        await asyncio.gather(reopen_task, purge_task, return_exceptions=True)

    assert reopen_task.exception() is None
    assert purge_task.result() is False, "the purge destroyed an account that had been reopened"
    acct = await _account(admin)
    assert acct is not None and (acct.state, acct.closed_at) == (AccountState.verified, None)
    inst = await _instance(admin)
    assert inst is not None and inst.state == LucielState.active


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_restore_in_flight_makes_the_grace_expiry_wait_and_then_do_nothing(monkeypatch):
    """The same boundary for the Luciel alone: Restore on the last day against the
    grace-expiry sweep. The sweep must find the Luciel ``active`` and leave it."""
    admin, _ = await _seed()
    async with tenant_session(admin) as s:
        await state_machine.delete_luciel(s, admin, admin)
    inst = await _instance(admin)
    assert inst is not None and inst.grace_window_started_at is not None
    deadline = grace_expiry.grace_window_deadline(inst.grace_window_started_at)

    holding, release = asyncio.Event(), asyncio.Event()
    real_requalify = state_machine._requalify_crm_links

    async def _slow_requalify(session, instance, **kwargs):
        holding.set()  # the instance row is locked by now
        await release.wait()
        return await real_requalify(session, instance, **kwargs)

    monkeypatch.setattr(state_machine, "_requalify_crm_links", _slow_requalify)

    async def _restore() -> None:
        async with tenant_session(admin) as s:
            await state_machine.restore(s, admin, admin)

    async def _expire() -> dict[str, int] | None:
        async with tenant_session(admin) as s:
            return await grace_expiry.expire_grace_window_if_due(
                s, admin_id=admin, now=deadline + timedelta(seconds=1)
            )

    restore_task = asyncio.create_task(_restore())
    await asyncio.wait_for(holding.wait(), timeout=20)
    expire_task = asyncio.create_task(_expire())
    done, _ = await asyncio.wait({expire_task}, timeout=1.5)
    try:
        assert not done, "the sweep judged the Luciel without waiting for the restore"
    finally:
        release.set()
        await asyncio.gather(restore_task, expire_task, return_exceptions=True)

    assert restore_task.exception() is None
    assert expire_task.result() is None, "the sweep destroyed a Luciel that had been restored"
    inst = await _instance(admin)
    assert inst is not None and inst.state == LucielState.active


# --------------------------------------------------------------------------- #
# Billing                                                                      #
# --------------------------------------------------------------------------- #
@pytest.fixture
def gateway(monkeypatch) -> FakeStripeGateway:
    """The fake as the process gateway, with the metered price bound (it gates on the
    price exactly as the real gateway does — unbound means no subscription at all)."""
    monkeypatch.setattr(get_settings(), "stripe_payg_price_id", PAYG_PRICE)
    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    yield gw
    set_stripe_gateway(None)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_billing_resumes_after_a_reopen_and_bills_only_what_is_new(
    gateway: FakeStripeGateway,
):
    """Closure settled 2 blocks and ended the metered subscription with its final
    invoice. After the reopen the card, the customer and ``payg_enabled`` are all
    still there, so the next billable block binds a NEW subscription and posts exactly
    one block — the two the final invoice collected are not billed again, and the
    counted conversations do not buy a second free 50."""
    admin, iid = await _seed()
    customer = f"cus_fake_{admin.hex[:16]}"
    async with tenant_session(admin) as s:
        s.add(
            ConversationBudget(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                billing_period_start=STRIPE_PERIOD_START.date(),
                counted_conversations=240,
                counted_session_ids=[str(uuid.uuid4()) for _ in range(240)],
            )
        )
    async with tenant_session(admin) as s:
        billing = await metering.get_or_create_billing_account(s, admin_id=admin)
        billing.billing_state = "payg_enabled"
        billing.stripe_customer_id = customer
        billing.period_start = STRIPE_PERIOD_START
    async with tenant_session(admin) as s:
        assert await metering.report_usage_after_count(s, admin_id=admin, instance_id=iid) == 2
    async with tenant_session(admin) as s:
        first_sub = (
            await metering.get_or_create_billing_account(s, admin_id=admin)
        ).stripe_subscription_id
    assert first_sub is not None

    await _close(admin)
    assert gateway.cancelled_subscriptions == [(first_sub, True)]
    async with tenant_session(admin) as s:
        billing = await metering.get_or_create_billing_account(s, admin_id=admin)
        assert billing.stripe_subscription_id is None
        assert billing.billing_state == "payg_enabled"

    async with tenant_session(admin) as s:
        outcome = await reopen.reopen_account(s, admin, admin)
    assert outcome.billing_state == "payg_enabled"
    assert outcome.subscription == reopen.SUBSCRIPTION_REBINDS
    assert outcome.luciel_restored is True

    # Still inside the settled blocks: nothing is owed, nothing is bound.
    async with tenant_session(admin) as s:
        assert await metering.report_usage_after_count(s, admin_id=admin, instance_id=iid) == 0
    # Conversation 350 opens block 3.
    async with tenant_session(admin) as s:
        await s.execute(
            update(ConversationBudget)
            .where(ConversationBudget.admin_id == admin, ConversationBudget.instance_id == iid)
            .values(counted_conversations=350)
        )
    async with tenant_session(admin) as s:
        assert await metering.report_usage_after_count(s, admin_id=admin, instance_id=iid) == 1
    async with tenant_session(admin) as s:
        billing = await metering.get_or_create_billing_account(s, admin_id=admin)
        assert billing.stripe_subscription_id is not None, "the metered subscription is back"
        assert (billing.accrued_blocks, billing.last_reported_blocks) == (3, 3)
    assert gateway.billed_quantity(customer) == 3, "2 settled at closure + 1 new, never 5"
    assert gateway.subscription_price(billing.stripe_subscription_id) == PAYG_PRICE


@pytest.mark.parametrize("shape", ["basil_parent", "legacy_top_level"])
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_closure_final_invoice_does_not_hand_a_reopen_a_second_free_fifty(
    gateway: FakeStripeGateway, shape: str
):
    """Live posture: the closure's ``cancel(invoice_now=True)`` makes Stripe issue and
    charge a final invoice, and its ``invoice.paid`` (period_end = the cancel instant)
    reaches ``handle_event`` while the account is closed. That used to move the anchor
    to the cancel instant and zero the watermarks, so after a reopen conversations
    241-250+ counted from zero again: a second free 50 inside the period already used.
    The fake emits no events, so this test pushes the one Stripe sends."""
    from app.billing import webhooks as billing_webhooks
    from app.billing.stripe_gateway import StripeEvent

    admin, iid = await _seed()
    customer = f"cus_fake_{admin.hex[:16]}"
    # The subscription's cycle began on the 1st; the closure lands later in it.
    gateway.now = STRIPE_PERIOD_START
    async with tenant_session(admin) as s:
        s.add(
            ConversationBudget(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                billing_period_start=STRIPE_PERIOD_START.date(),
                counted_conversations=250,
                counted_session_ids=[str(uuid.uuid4()) for _ in range(250)],
            )
        )
    async with tenant_session(admin) as s:
        billing = await metering.get_or_create_billing_account(s, admin_id=admin)
        billing.billing_state = "payg_enabled"
        billing.stripe_customer_id = customer
        billing.period_start = STRIPE_PERIOD_START
    async with tenant_session(admin) as s:
        assert await metering.report_usage_after_count(s, admin_id=admin, instance_id=iid) == 2
    async with tenant_session(admin) as s:
        billing = await metering.get_or_create_billing_account(s, admin_id=admin)
        first_sub = billing.stripe_subscription_id
        resets_before = billing.period_resets_at
    assert first_sub is not None
    assert billing.period_start == STRIPE_PERIOD_START
    gateway.now = None  # from here the fake's clock is the real one (a later rebind)

    await _close(admin)
    # The final invoice Stripe issues for the cancelled subscription, paid.
    cancel_instant = max(datetime.now(UTC), STRIPE_PERIOD_START + timedelta(days=1))
    invoice: dict[str, object] = {
        "object": "invoice",
        "customer": customer,
        "period_start": int(STRIPE_PERIOD_START.timestamp()),
        "period_end": int(cancel_instant.timestamp()),
    }
    if shape == "basil_parent":
        invoice["parent"] = {
            "type": "subscription_details",
            "subscription_details": {"subscription": first_sub},
        }
    else:
        invoice["subscription"] = first_sub
    async with tenant_session(admin) as s:
        action = await billing_webhooks.handle_event(
            s,
            admin_id=admin,
            event=StripeEvent(event_id=f"evt_final_{shape}", type="invoice.paid", data=invoice),
        )
    assert action == "invoice_paid"
    async with tenant_session(admin) as s:
        billing = await metering.get_or_create_billing_account(s, admin_id=admin)
        assert billing.period_start == STRIPE_PERIOD_START, "the final invoice moved the anchor"
        assert billing.period_resets_at == resets_before
        assert (billing.accrued_blocks, billing.last_reported_blocks) == (2, 2)
        assert billing.accrual_period_start == STRIPE_PERIOD_START.date()

    async with tenant_session(admin) as s:
        outcome = await reopen.reopen_account(s, admin, admin)
    assert outcome.subscription == reopen.SUBSCRIPTION_REBINDS

    # Conversation 251 is the first of block 3: billed, not the first of a fresh 50.
    async with tenant_session(admin) as s:
        await s.execute(
            update(ConversationBudget)
            .where(ConversationBudget.admin_id == admin, ConversationBudget.instance_id == iid)
            .values(counted_conversations=251)
        )
    async with tenant_session(admin) as s:
        assert await metering.report_usage_after_count(s, admin_id=admin, instance_id=iid) == 1
    assert gateway.billed_quantity(customer) == 3, "2 settled at closure + block 3"
    async with tenant_session(admin) as s:
        counted = (
            await s.execute(
                select(ConversationBudget.counted_conversations).where(
                    ConversationBudget.admin_id == admin
                )
            )
        ).scalar_one()
    assert counted == 251, "the reopened account kept its period's count"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_invoice_for_the_bound_subscription_still_advances_the_period(
    gateway: FakeStripeGateway,
):
    """The ended-subscription rule is narrow: the cycle invoice of the subscription the
    account currently holds is the period boundary, exactly as before."""
    from app.billing import webhooks as billing_webhooks
    from app.billing.stripe_gateway import StripeEvent

    admin, _ = await _seed(luciel=None)
    next_start = STRIPE_PERIOD_START + timedelta(days=31)
    async with tenant_session(admin) as s:
        billing = await metering.get_or_create_billing_account(s, admin_id=admin)
        billing.billing_state = "payg_enabled"
        billing.stripe_customer_id = "cus_cycle"
        billing.stripe_subscription_id = "sub_current"
        billing.period_start = STRIPE_PERIOD_START
    async with tenant_session(admin) as s:
        await billing_webhooks.handle_event(
            s,
            admin_id=admin,
            event=StripeEvent(
                event_id="evt_cycle_current",
                type="invoice.paid",
                data={
                    "object": "invoice",
                    "customer": "cus_cycle",
                    "parent": {"subscription_details": {"subscription": "sub_current"}},
                    "period_start": int(STRIPE_PERIOD_START.timestamp()),
                    "period_end": int(next_start.timestamp()),
                },
            ),
        )
    async with tenant_session(admin) as s:
        billing = await metering.get_or_create_billing_account(s, admin_id=admin)
        assert billing.period_start == next_start


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_reopen_names_a_subscription_the_closure_could_not_end(gateway: FakeStripeGateway):
    """Stripe refused the cancel at closure: the subscription never stopped, the
    pointer was kept, and the reopen reports it as live rather than "will rebind"."""
    admin, _ = await _seed(luciel=None)
    async with tenant_session(admin) as s:
        billing = await metering.get_or_create_billing_account(s, admin_id=admin)
        billing.billing_state = "payg_enabled"
        billing.stripe_customer_id = "cus_unknown_to_the_fake"
        billing.stripe_subscription_id = "sub_unknown_to_the_fake"
    await _close(admin)
    assert gateway.cancelled_subscriptions == []

    async with tenant_session(admin) as s:
        outcome = await reopen.reopen_account(s, admin, admin)
    assert (outcome.billing_state, outcome.subscription) == (
        "payg_enabled",
        reopen.SUBSCRIPTION_LIVE,
    )
    assert (outcome.luciel_restored, outcome.luciel_state) == (False, None)
