"""Metering + billing-account + webhook + idempotent-reporting integration.

Deterministic FakeStripeGateway only — NO real Stripe network call (HARD
CONSTRAINT #1). Exercises build_budget across free_cap/at_cap/payg states, the
Stripe-webhook-driven transitions (invoice.paid reset, payment_failed dunning,
day-7 reduced, pm attach/detach), idempotent redelivery, and idempotent metered
reporting (no double-bill).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta

import pytest
from app.billing import dunning, metering
from app.billing import webhooks as billing_webhooks
from app.billing.stripe_gateway import FakeStripeGateway, StripeEvent
from app.db.session import tenant_session
from app.models.budget import ConversationBudget
from app.models.instance import Account, AccountState, Instance, LucielState
from app.runtime import budget
from app.runtime.budget import current_billing_period_start

# The Stripe anchor and the counter row must name the SAME period (P0-4) — a report keyed
# on a period the counter was not written under reads an empty counter.
STRIPE_PERIOD_START = datetime.combine(current_billing_period_start(), time.min, tzinfo=UTC)


async def _seed_tenant(admin: uuid.UUID, counted: int = 0) -> uuid.UUID:
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
    return iid


def _settled_calendar_period() -> dict[str, int]:
    """The period an ``invoice.paid`` settles, stated the way Stripe states it.

    The counter rows these tests seed are keyed on the calendar-month fallback, so the
    invoice that settles them covers that month and the NEXT period starts on the next
    month's first day. A period-less event falls back to "the next period starts now",
    whose date equals the fallback key on the 1st of a month — the reset asserted here
    then reads the very row it seeded (the nightly went red on 2026-10-01, round 7).
    """
    now = datetime.now(UTC)
    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    end = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=UTC)
    return {"period_start": int(start.timestamp()), "period_end": int(end.timestamp())}


@pytest.mark.usefixtures("db_engine_reset")
async def test_free_cap_below_50_not_at_cap() -> None:
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, counted=10)
    async with tenant_session(admin) as s:
        b = await metering.build_budget(s, admin_id=admin, instance_id=iid)
    assert b.billing_state == "free_cap"
    assert b.at_cap is False
    assert b.free_allowance == 50
    assert b.billed_this_period == 0


@pytest.mark.usefixtures("db_engine_reset")
async def test_at_50_no_card_is_at_cap() -> None:
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, counted=50)
    async with tenant_session(admin) as s:
        b = await metering.build_budget(s, admin_id=admin, instance_id=iid)
    assert b.at_cap is True  # at_cap is DATA (§3.4.1b), not an error
    assert b.billed_this_period == 0  # cardless → nothing billed


@pytest.mark.usefixtures("db_engine_reset")
async def test_payg_above_50_billed_not_at_cap() -> None:
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, counted=240)
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        acct.billing_state = "payg_enabled"
        acct.stripe_subscription_id = "sub_test"
        await s.flush()
    async with tenant_session(admin) as s:
        b = await metering.build_budget(s, admin_id=admin, instance_id=iid)
    assert b.at_cap is False
    assert b.billed_this_period == 190  # 240 - 50 (frozen seed)
    assert b.billed_blocks == 2  # ceil(190/100)
    assert b.billed_cents == 7800  # 2 × $39


@pytest.mark.usefixtures("db_engine_reset")
async def test_invoice_paid_resets_counter_no_rollover() -> None:
    """The invoice settles the period it names; the NEXT period (its ``period_end``)
    starts at zero — and the settled period's ledger is kept as the invoice's evidence
    (audit F005/F042), not deleted."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, counted=120)
    async with tenant_session(admin) as s:
        await metering.get_or_create_billing_account(s, admin_id=admin)
    settled_end = datetime(2030, 2, 15, tzinfo=UTC)
    event = StripeEvent(
        event_id="evt_1",
        type="invoice.paid",
        data={
            "period_start": int(datetime(2030, 1, 15, tzinfo=UTC).timestamp()),
            "period_end": int(settled_end.timestamp()),
        },
    )
    async with tenant_session(admin) as s:
        action = await billing_webhooks.handle_event(s, admin_id=admin, event=event)
    assert action == "invoice_paid"
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        assert acct.period_start == settled_end  # the NEW period begins where the paid one ended
        assert acct.accrual_period_start == settled_end.date()
        b = await metering.build_budget(
            s, admin_id=admin, instance_id=iid, now=settled_end + timedelta(days=5)
        )
        assert b.conversations_this_period == 0  # reset, NO rollover
        from sqlalchemy import func as sa_func
        from sqlalchemy import select as sa_select

        kept = (
            await s.execute(
                sa_select(sa_func.count())
                .select_from(ConversationBudget)
                .where(ConversationBudget.admin_id == admin)
            )
        ).scalar_one()
        assert kept == 1, "the settled period's ledger row is the evidence behind the invoice"


@pytest.mark.usefixtures("db_engine_reset")
async def test_webhook_redelivery_is_idempotent() -> None:
    admin = uuid.uuid4()
    await _seed_tenant(admin, counted=10)
    event = StripeEvent(event_id="evt_dup", type="invoice.payment_failed", data={})
    async with tenant_session(admin) as s:
        first = await billing_webhooks.handle_event(s, admin_id=admin, event=event)
    async with tenant_session(admin) as s:
        second = await billing_webhooks.handle_event(s, admin_id=admin, event=event)
    assert first == "payment_failed"
    assert second == "duplicate"  # redelivery no-op


@pytest.mark.usefixtures("db_engine_reset")
async def test_payment_failed_drives_dunning_retrying() -> None:
    admin = uuid.uuid4()
    await _seed_tenant(admin, counted=10)
    event = StripeEvent(event_id="evt_pf", type="invoice.payment_failed", data={})
    async with tenant_session(admin) as s:
        await billing_webhooks.handle_event(s, admin_id=admin, event=event)
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        assert acct.dunning_state == "retrying"
        assert acct.dunning_started_at is not None


@pytest.mark.usefixtures("db_engine_reset")
async def test_day7_payment_failed_goes_reduced_free_cap() -> None:
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, counted=80)
    # Card on file (payg) but dunning will reduce → behaves as free-cap, at_cap true.
    started = datetime.now(UTC) - timedelta(days=7)
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        acct.billing_state = "payg_enabled"
        acct.dunning_state = "retrying"
        acct.dunning_started_at = started
        await s.flush()
    event = StripeEvent(event_id="evt_pf7", type="invoice.payment_failed", data={})
    async with tenant_session(admin) as s:
        await billing_webhooks.handle_event(s, admin_id=admin, event=event)
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        assert acct.dunning_state == "reduced"
    async with tenant_session(admin) as s:
        b = await metering.build_budget(s, admin_id=admin, instance_id=iid)
    # reduced = free-cap behavior: over 50 with no live card → at_cap, nothing billed.
    assert b.at_cap is True
    assert b.billed_this_period == 0


@pytest.mark.usefixtures("db_engine_reset")
async def test_pm_attach_detach_flips_billing_state() -> None:
    admin = uuid.uuid4()
    await _seed_tenant(admin, counted=10)
    attach = StripeEvent(
        event_id="evt_attach",
        type="payment_method.attached",
        data={"card": {"brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2030}},
    )
    async with tenant_session(admin) as s:
        await billing_webhooks.handle_event(s, admin_id=admin, event=attach)
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        assert acct.billing_state == "payg_enabled"
        assert acct.pm_brand == "visa"
        assert acct.pm_last4 == "4242"
    detach = StripeEvent(event_id="evt_detach", type="payment_method.detached", data={})
    async with tenant_session(admin) as s:
        await billing_webhooks.handle_event(s, admin_id=admin, event=detach)
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        assert acct.billing_state == "free_cap"
        assert acct.pm_brand is None

    # Axis3-F04: each billing state change emits the canonical lifecycle_transition.
    from app.models.audit import AdminAuditLog
    from sqlalchemy import select as sa_select

    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    sa_select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "lifecycle_transition",
                    )
                )
            )
            .scalars()
            .all()
        )
        transitions = {
            (r.event_payload.get("from"), r.event_payload.get("to"))
            for r in rows
            if r.event_payload.get("trigger") == "payment_method"
        }
        assert ("free_cap", "payg_enabled") in transitions
        assert ("payg_enabled", "free_cap") in transitions


@pytest.mark.usefixtures("db_engine_reset")
async def test_idempotent_metered_reporting_no_double_bill() -> None:
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, counted=240)
    gw = FakeStripeGateway()
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        acct.billing_state = "payg_enabled"
        acct.stripe_customer_id = "cus_test"  # the meter event is keyed on the customer
        acct.stripe_subscription_id = "sub_test"
        acct.period_start = STRIPE_PERIOD_START
        await s.flush()
    # Report twice over the same watermark span — the second finds nothing owing.
    async with tenant_session(admin) as s:
        q1 = await metering.report_usage(s, admin_id=admin, instance_id=iid, gateway=gw)
    async with tenant_session(admin) as s:
        q2 = await metering.report_usage(s, admin_id=admin, instance_id=iid, gateway=gw)
    assert q1 == 2  # 2 blocks
    assert q2 == 0  # nothing new owed, so Stripe is not called again
    # One event, and the meter totals the real usage rather than twice it.
    assert len(gw.meter_events) == 1
    assert gw.billed_quantity("cus_test") == 2


# ---------------------------------------------------------------------------
# Regression tests for Axis8-F02, Axis8-F03, Axis8-F04
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_payment_failed_emits_lifecycle_notice_sent():
    """Axis8-F03: invoice.payment_failed must emit lifecycle_notice_sent audit event."""
    from app.audit.log import AdminAuditLog
    from sqlalchemy import select as sa_select

    admin = uuid.uuid4()
    await _seed_tenant(admin)
    now = datetime(2026, 6, 20, tzinfo=UTC)
    event = StripeEvent(event_id="evt_test_f03", type="invoice.payment_failed", data={})

    async with tenant_session(admin) as s:
        action = await billing_webhooks.handle_event(s, admin_id=admin, event=event, now=now)
    assert action == "payment_failed"

    async with tenant_session(admin) as s:
        notice = (
            await s.execute(
                sa_select(AdminAuditLog).where(
                    AdminAuditLog.admin_id == admin,
                    AdminAuditLog.event_type == "lifecycle_notice_sent",
                )
            )
        ).scalar_one_or_none()
        assert notice is not None, "lifecycle_notice_sent event must be emitted on payment failure"
        assert notice.event_payload.get("notice_type") == "payment_failed_retry"


# The renewal the Axis8-F02 tests run: the Stripe period that ends, the start of the one
# the renewal opens, and that period's end. Every read passes its own ``now`` and the
# conversations are seeded rows, so the tests do not depend on the day they run.
_RENEWED_FROM = datetime(2030, 1, 15, tzinfo=UTC)
_RENEWED_TO = datetime(2030, 2, 15, tzinfo=UTC)
_RENEWED_UNTIL = datetime(2030, 3, 15, tzinfo=UTC)
# A moment inside [_RENEWED_FROM, _RENEWED_TO), for invoices paid mid-cycle.
_MID_PERIOD = datetime(2030, 1, 25, 9, 30, tzinfo=UTC)

# The shapes an invoice names its subscription in. A webhook payload follows the API
# version set on the endpoint in the Stripe Dashboard, not STRIPE_API_VERSION, and the
# repository does not record that version: 2025-03-31.basil and later put the
# subscription under parent.subscription_details, earlier versions at the top level, as
# an id or expanded (webhooks._invoice_subscription_id reads all three).
_INVOICE_SHAPES = ("parent_subscription_details", "top_level_id", "top_level_expanded")


def _naming(shape: str, subscription: str) -> dict[str, object]:
    """The invoice fields that name ``subscription`` in ``shape``."""
    if shape == "parent_subscription_details":
        return {
            "parent": {
                "type": "subscription_details",
                "subscription_details": {"subscription": subscription},
            }
        }
    if shape == "top_level_id":
        return {"subscription": subscription}
    if shape == "top_level_expanded":
        return {"subscription": {"object": "subscription", "id": subscription}}
    raise ValueError(shape)


async def _seed_renewing_tenant(admin: uuid.UUID, *, counted: int) -> uuid.UUID:
    """A PAYG tenant in the Stripe period [_RENEWED_FROM, _RENEWED_TO) with ``counted``
    conversations in it. Two blocks have accrued and only the first was reported (the
    second crossed after the last report), so the renewal has a residual to settle."""
    iid = await _seed_tenant(admin)
    async with tenant_session(admin) as s:
        s.add(
            ConversationBudget(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                billing_period_start=_RENEWED_FROM.date(),
                counted_conversations=counted,
                counted_session_ids=[str(uuid.uuid4()) for _ in range(counted)],
            )
        )
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        acct.billing_state = "payg_enabled"
        acct.stripe_customer_id = "cus_renewing"
        acct.stripe_subscription_id = "sub_renewing"
        acct.period_start = _RENEWED_FROM
        acct.period_resets_at = _RENEWED_TO
        acct.accrual_period_start = _RENEWED_FROM.date()
        acct.accrued_blocks = 2
        acct.last_reported_blocks = 1
        await s.flush()
    return iid


def _renewal_invoice(shape: str = "parent_subscription_details") -> dict[str, object]:
    """The renewal invoice of [_RENEWED_FROM, _RENEWED_TO): the period it bills, the
    reason it exists, and the subscription, named in ``shape``."""
    return {
        "object": "invoice",
        "customer": "cus_renewing",
        "billing_reason": "subscription_cycle",
        **_naming(shape, "sub_renewing"),
        "period_start": int(_RENEWED_FROM.timestamp()),
        "period_end": int(_RENEWED_TO.timestamp()),
        "status": "paid",
    }


def _new_period_conversations(admin: uuid.UUID, iid: uuid.UUID, counted: int) -> ConversationBudget:
    """``counted`` conversations in the period the renewal opened."""
    return ConversationBudget(
        id=uuid.uuid4(),
        admin_id=admin,
        instance_id=iid,
        billing_period_start=_RENEWED_TO.date(),
        counted_conversations=counted,
        counted_session_ids=[str(uuid.uuid4()) for _ in range(counted)],
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize("shape", _INVOICE_SHAPES)
async def test_a_renewal_resets_the_period_once_even_on_replay(shape: str) -> None:
    """Axis8-F02: a subscription renewal starts the next period at zero, once.

    This test used to send ``customer.subscription.renewed``, which Stripe does not send.
    The counter rolls at the cycle end by time (``current_period``, F030), before any
    webhook: Stripe does not try to collect the renewal invoice until an hour after
    creating it. Of the events the renewal then produces, ``customer.subscription.updated``
    and ``invoice.payment_succeeded`` change nothing. ``invoice.paid`` for the renewal
    invoice (``billing_reason`` ``subscription_cycle``) moves the stored anchor to the
    invoice's ``period_end`` and takes the new period's end from Stripe, posts the
    settled period's unreported block under that period's own identifier, starts the
    watermarks again at zero and keeps the settled period's ledger row. The same invoice
    delivered again, under the same event id or as a separate event (Stripe documents
    both), changes nothing: the new period's block is not posted a second time.
    """
    from app.billing.stripe_gateway import set_stripe_gateway
    from app.models.audit import AdminAuditLog
    from sqlalchemy import select as sa_select

    admin = uuid.uuid4()
    iid = await _seed_renewing_tenant(admin, counted=180)
    advanced = StripeEvent(
        event_id="evt_renewal_subscription_updated",
        type="customer.subscription.updated",
        data={
            "object": "subscription",
            "id": "sub_renewing",
            "customer": "cus_renewing",
            "status": "active",
            "items": {
                "object": "list",
                "data": [
                    {
                        "object": "subscription_item",
                        "current_period_start": int(_RENEWED_TO.timestamp()),
                        "current_period_end": int(_RENEWED_UNTIL.timestamp()),
                    }
                ],
            },
        },
    )
    paid = StripeEvent(
        event_id="evt_renewal_paid", type="invoice.paid", data=_renewal_invoice(shape)
    )
    succeeded = StripeEvent(
        event_id="evt_renewal_payment_succeeded",
        type="invoice.payment_succeeded",
        data=_renewal_invoice(shape),
    )
    # The same invoice as a second, separate event.
    paid_again = StripeEvent(
        event_id="evt_renewal_paid_again", type="invoice.paid", data=_renewal_invoice(shape)
    )
    gw = FakeStripeGateway()
    gw.subscription_periods["sub_renewing"] = (_RENEWED_TO, _RENEWED_UNTIL)

    async def marks() -> tuple[object, ...]:
        async with tenant_session(admin) as s:
            acct = await metering.get_or_create_billing_account(s, admin_id=admin)
            return (
                acct.period_start,
                acct.period_resets_at,
                acct.accrued_blocks,
                acct.last_reported_blocks,
                acct.accrual_period_start,
            )

    async def budget_at(now: datetime) -> tuple[int, datetime]:
        async with tenant_session(admin) as s:
            b = await metering.build_budget(s, admin_id=admin, instance_id=iid, now=now)
            return b.conversations_this_period, b.period_resets_at

    async def report_at(now: datetime) -> int:
        async with tenant_session(admin) as s:
            return await metering.report_usage(
                s, admin_id=admin, instance_id=iid, now=now, gateway=gw
            )

    set_stripe_gateway(gw)
    try:
        assert await budget_at(_RENEWED_TO - timedelta(days=1)) == (180, _RENEWED_TO)
        # The cycle has ended and no webhook has come: the counter is already on the new
        # period, while the stored anchor still names the old one.
        assert await budget_at(_RENEWED_TO + timedelta(minutes=5)) == (0, _RENEWED_UNTIL)
        assert (await marks())[:2] == (_RENEWED_FROM, _RENEWED_TO)

        async with tenant_session(admin) as s:
            assert await billing_webhooks.handle_event(s, admin_id=admin, event=advanced) == (
                "ignored"
            )
        assert await marks() == (_RENEWED_FROM, _RENEWED_TO, 2, 1, _RENEWED_FROM.date())

        async with tenant_session(admin) as s:
            assert await billing_webhooks.handle_event(s, admin_id=admin, event=paid) == (
                "invoice_paid"
            )
        async with tenant_session(admin) as s:
            assert await billing_webhooks.handle_event(s, admin_id=admin, event=succeeded) == (
                "ignored"
            )
        assert await marks() == (_RENEWED_TO, _RENEWED_UNTIL, 0, 0, _RENEWED_TO.date())

        # The tenant runs 60 conversations in the new period: its first block is posted.
        async with tenant_session(admin) as s:
            s.add(_new_period_conversations(admin, iid, 60))
        assert await report_at(_RENEWED_TO + timedelta(days=5)) == 1

        # Redelivered under the same event id, the invoice is not processed at all...
        async with tenant_session(admin) as s:
            assert await billing_webhooks.handle_event(s, admin_id=admin, event=paid) == (
                "duplicate"
            )
        # ...and processed again as a separate event, it settles a period already passed:
        # the watermarks are not zeroed, so the block is not posted a second time.
        async with tenant_session(admin) as s:
            assert await billing_webhooks.handle_event(s, admin_id=admin, event=paid_again) == (
                "invoice_paid"
            )
        assert await report_at(_RENEWED_TO + timedelta(days=6)) == 0
        assert await marks() == (_RENEWED_TO, _RENEWED_UNTIL, 1, 1, _RENEWED_TO.date())
        assert await budget_at(_RENEWED_TO + timedelta(days=6)) == (60, _RENEWED_UNTIL)
    finally:
        set_stripe_gateway(None)

    async with tenant_session(admin) as s:
        ledger = (
            await s.execute(
                sa_select(
                    ConversationBudget.billing_period_start,
                    ConversationBudget.counted_conversations,
                ).where(ConversationBudget.admin_id == admin)
            )
        ).all()
        assert dict(ledger) == {_RENEWED_FROM.date(): 180, _RENEWED_TO.date(): 60}
        resets = (
            (
                await s.execute(
                    sa_select(AdminAuditLog.event_payload)
                    .where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "invoice_paid",
                    )
                    .order_by(AdminAuditLog.seq)
                )
            )
            .scalars()
            .all()
        )
        assert [(p["reset"], p["previous_period_start"], p["period_start"]) for p in resets] == [
            ("period_advanced", "2030-01-15", "2030-02-15"),
            ("none", "2030-02-15", "2030-02-15"),
        ]
    # The settled period's second block under the settled period's key, then the new
    # period's first block under the new period's key, each once.
    cus = "cus_renewing"
    assert [(e.value, e.identifier) for e in gw.meter_events] == [
        (1, f"usage:{cus}:2030-01-15:1-2"),
        (1, f"usage:{cus}:2030-02-15:0-1"),
    ]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize("shape", _INVOICE_SHAPES)
async def test_an_ended_subscriptions_final_invoice_is_read_in_every_shape(
    shape: str,
) -> None:
    """The subscription an invoice bills decides whether it can end the period at all, so
    every shape that names it must be read. A shape read as naming nothing would make a
    cancelled subscription's final invoice look like this account's renewal: the anchor
    would jump to the cancellation instant with a second free 50 (round 7 money review,
    finding 0). The invoice carries no billing_reason, so only the subscription it names
    keeps it from being read as the renewal."""
    from app.billing.stripe_gateway import set_stripe_gateway

    admin = uuid.uuid4()
    iid = await _seed_renewing_tenant(admin, counted=180)
    final_invoice = StripeEvent(
        event_id=f"evt_final_{shape}",
        type="invoice.paid",
        data={
            "object": "invoice",
            "customer": "cus_renewing",
            **_naming(shape, "sub_cancelled"),
            "period_start": int(_RENEWED_FROM.timestamp()),
            "period_end": int(_MID_PERIOD.timestamp()),
            "status": "paid",
        },
    )
    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    try:
        async with tenant_session(admin) as s:
            action = await billing_webhooks.handle_event(s, admin_id=admin, event=final_invoice)
    finally:
        set_stripe_gateway(None)

    assert action == "invoice_paid"
    assert gw.meter_events == []
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        assert (acct.period_start, acct.period_resets_at) == (_RENEWED_FROM, _RENEWED_TO)
        assert (acct.accrued_blocks, acct.last_reported_blocks) == (2, 1)
        b = await metering.build_budget(
            s, admin_id=admin, instance_id=iid, now=_MID_PERIOD + timedelta(days=1)
        )
        assert b.conversations_this_period == 180


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_customer_subscription_renewed_is_not_treated_as_a_renewal() -> None:
    """Axis8-F02's old trigger. Stripe's customer.subscription.* events are created,
    deleted, paused, pending_update_applied, pending_update_expired, resumed,
    trial_will_end and updated. There is no ``renewed``, so the branch that handled it
    never ran. An event of that type is acknowledged and ignored, even when it carries
    an invoice's period fields. The period does not end, no residual is posted, the
    watermarks stay, and dunning is not cleared, because nothing was paid."""
    from app.billing.stripe_gateway import set_stripe_gateway
    from app.models.audit import AdminAuditLog
    from sqlalchemy import select as sa_select

    admin = uuid.uuid4()
    iid = await _seed_renewing_tenant(admin, counted=180)
    dunning_since = datetime(2030, 2, 10, tzinfo=UTC)
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        acct.dunning_state = dunning.RETRYING
        acct.dunning_started_at = dunning_since
        await s.flush()
    event = StripeEvent(
        event_id="evt_not_a_renewal",
        type="customer.subscription.renewed",
        data={
            "object": "subscription",
            "id": "sub_renewing",
            "customer": "cus_renewing",
            "period_start": int(_RENEWED_FROM.timestamp()),
            "period_end": int(_RENEWED_TO.timestamp()),
        },
    )

    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    try:
        async with tenant_session(admin) as s:
            action = await billing_webhooks.handle_event(s, admin_id=admin, event=event)
    finally:
        set_stripe_gateway(None)

    assert action == "ignored"
    assert gw.meter_events == []
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        assert (acct.period_start, acct.period_resets_at) == (_RENEWED_FROM, _RENEWED_TO)
        assert (acct.accrued_blocks, acct.last_reported_blocks, acct.accrual_period_start) == (
            2,
            1,
            _RENEWED_FROM.date(),
        )
        assert (acct.dunning_state, acct.dunning_started_at) == (dunning.RETRYING, dunning_since)
        b = await metering.build_budget(
            s, admin_id=admin, instance_id=iid, now=_RENEWED_TO - timedelta(days=1)
        )
        assert b.conversations_this_period == 180
        paid = (
            await s.execute(
                sa_select(AdminAuditLog.event_id).where(
                    AdminAuditLog.admin_id == admin,
                    AdminAuditLog.event_type == "invoice_paid",
                )
            )
        ).all()
        assert paid == []


def _mid_period_invoice(billing_reason: str, subscription: str | None) -> dict[str, object]:
    """A paid invoice created mid-period. Stripe's own one-off example in the Invoice
    object reference has ``"billing_reason": "manual"``, ``"parent": null`` and
    ``period_start == period_end`` == the moment it was created."""
    return {
        "object": "invoice",
        "customer": "cus_renewing",
        "billing_reason": billing_reason,
        **(
            _naming("parent_subscription_details", subscription)
            if subscription is not None
            else {"parent": None}
        ),
        "period_start": int(_MID_PERIOD.timestamp()),
        "period_end": int(_MID_PERIOD.timestamp()),
        "status": "paid",
    }


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize(
    "invoice",
    [
        pytest.param(_mid_period_invoice("manual", None), id="one_off"),
        pytest.param(_mid_period_invoice("subscription_update", "sub_renewing"), id="update"),
        pytest.param(_mid_period_invoice("subscription_threshold", "sub_renewing"), id="threshold"),
        pytest.param(_mid_period_invoice("subscription_create", "sub_renewing"), id="creation"),
        # Axis8-F02's own renewal invoice with only the reason changed: the reason decides.
        pytest.param({**_renewal_invoice(), "billing_reason": "manual"}, id="renewal_shaped"),
    ],
)
async def test_a_paid_invoice_that_is_not_the_renewal_leaves_the_period_alone(
    invoice: dict[str, object],
) -> None:
    """Only the renewal invoice (``billing_reason`` ``subscription_cycle``) ends a Stripe
    period. Any other paid invoice used to start a new period at its ``period_end``:
    a one-off invoice paid on Jan 25 opened a second free 50 inside the Jan 15 - Feb 15
    period and zeroed the watermarks. The debt is settled (dunning clears), but the
    anchor, the period's end, the watermarks and the counter stay, and nothing is
    posted to the meter."""
    from app.billing.stripe_gateway import set_stripe_gateway
    from app.models.audit import AdminAuditLog
    from sqlalchemy import select as sa_select

    admin = uuid.uuid4()
    iid = await _seed_renewing_tenant(admin, counted=180)
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        acct.dunning_state = dunning.RETRYING
        acct.dunning_started_at = _MID_PERIOD - timedelta(days=2)
        await s.flush()
    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    try:
        async with tenant_session(admin) as s:
            action = await billing_webhooks.handle_event(
                s,
                admin_id=admin,
                event=StripeEvent(event_id="evt_not_renewal", type="invoice.paid", data=invoice),
            )
    finally:
        set_stripe_gateway(None)

    assert action == "invoice_paid"
    assert gw.meter_events == []
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        assert (acct.period_start, acct.period_resets_at) == (_RENEWED_FROM, _RENEWED_TO)
        assert (acct.accrued_blocks, acct.last_reported_blocks, acct.accrual_period_start) == (
            2,
            1,
            _RENEWED_FROM.date(),
        )
        assert (acct.dunning_state, acct.dunning_started_at) == (dunning.NONE, None)
        b = await metering.build_budget(
            s, admin_id=admin, instance_id=iid, now=_MID_PERIOD + timedelta(days=1)
        )
        assert (b.conversations_this_period, b.period_resets_at) == (180, _RENEWED_TO)
        payloads = (
            (
                await s.execute(
                    sa_select(AdminAuditLog.event_payload).where(
                        AdminAuditLog.admin_id == admin,
                        AdminAuditLog.event_type == "invoice_paid",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [(p["reset"], p["reason"], p["billing_reason"]) for p in payloads] == [
            ("none", "not_a_renewal", invoice["billing_reason"])
        ]


class _SubscriptionReadFails(FakeStripeGateway):
    async def get_subscription_period(self, *, subscription_id: str):  # type: ignore[override]
        raise RuntimeError("stripe unavailable")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
@pytest.mark.parametrize("stripe_says", ["the_new_period", "nothing", "a_later_period", "error"])
async def test_a_month_end_renewal_ends_the_next_period_on_stripes_date(stripe_says: str) -> None:
    """A subscription anchored on Jan 31 renews on Feb 28, then on Mar 31: Stripe clamps
    the anchor's day to each month's length. The renewal's invoice.paid used to set the
    next end to add_months(Feb 28, 1) = Mar 28, so from Mar 28 the counter ran a period
    Stripe never opened, with a second free 50. The end now comes from Stripe's
    subscription. When Stripe cannot say, or already names a later period (the invoice
    was paid after the next renewal), it follows from the settled period's own bounds."""
    from app.billing.stripe_gateway import set_stripe_gateway

    jan31 = datetime(2030, 1, 31, 14, 5, tzinfo=UTC)
    feb28 = datetime(2030, 2, 28, 14, 5, tzinfo=UTC)
    mar31 = datetime(2030, 3, 31, 14, 5, tzinfo=UTC)
    apr30 = datetime(2030, 4, 30, 14, 5, tzinfo=UTC)
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        acct.billing_state = "payg_enabled"
        acct.stripe_customer_id = "cus_month_end"
        acct.stripe_subscription_id = "sub_month_end"
        acct.period_start = jan31  # the bounds the subscription had when it was bound
        acct.period_resets_at = feb28
        acct.accrual_period_start = jan31.date()
        await s.flush()
    gw = _SubscriptionReadFails() if stripe_says == "error" else FakeStripeGateway()
    if stripe_says == "the_new_period":
        gw.subscription_periods["sub_month_end"] = (feb28, mar31)
    elif stripe_says == "a_later_period":
        gw.subscription_periods["sub_month_end"] = (mar31, apr30)
    renewal = StripeEvent(
        event_id=f"evt_month_end_{stripe_says}",
        type="invoice.paid",
        data={
            "object": "invoice",
            "customer": "cus_month_end",
            "billing_reason": "subscription_cycle",
            "parent": {
                "type": "subscription_details",
                "subscription_details": {"subscription": "sub_month_end"},
            },
            "period_start": int(jan31.timestamp()),
            "period_end": int(feb28.timestamp()),
        },
    )
    set_stripe_gateway(gw)
    try:
        async with tenant_session(admin) as s:
            action = await billing_webhooks.handle_event(s, admin_id=admin, event=renewal)
    finally:
        set_stripe_gateway(None)
    assert action == "invoice_paid"

    async with tenant_session(admin) as s:
        s.add(
            ConversationBudget(
                id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                billing_period_start=feb28.date(),
                counted_conversations=70,
                counted_session_ids=[str(uuid.uuid4()) for _ in range(70)],
            )
        )
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        assert (acct.period_start, acct.period_resets_at) == (feb28, mar31)
        # Mar 29 is still inside Stripe's Feb 28 - Mar 31 period: the same 70 conversations.
        b = await metering.build_budget(
            s, admin_id=admin, instance_id=iid, now=datetime(2030, 3, 29, tzinfo=UTC)
        )
        assert (b.conversations_this_period, b.period_resets_at) == (70, mar31)
        # The next period opens on Stripe's renewal date and ends on the next one.
        b = await metering.build_budget(
            s, admin_id=admin, instance_id=iid, now=mar31 + timedelta(hours=1)
        )
        assert (b.conversations_this_period, b.period_resets_at) == (0, apr30)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_invoice_paid_restores_dormant_connections():
    """Axis8-F04: invoice.paid must restore dormant connections to unconfigured."""
    from app.models.connection import InstanceConnection
    from sqlalchemy import select as sa_select

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)

    # Seed a dormant connection.
    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="calendar",
                auth_class="oauth_token",
                provider="google",
                status="dormant",
            )
        )
        await s.flush()

    now = datetime(2026, 6, 20, tzinfo=UTC)
    event = StripeEvent(event_id="evt_inv_paid_f04", type="invoice.paid", data={})
    async with tenant_session(admin) as s:
        await billing_webhooks.handle_event(s, admin_id=admin, event=event, now=now)

    async with tenant_session(admin) as s:
        conn = (
            await s.execute(
                sa_select(InstanceConnection).where(
                    InstanceConnection.admin_id == admin,
                    InstanceConnection.connection_type == "calendar",
                )
            )
        ).scalar_one()
        assert conn.status == "unconfigured", (
            f"Dormant connection must be restored on invoice.paid; got {conn.status!r}"
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_pm_attached_restores_dormant_connections():
    """Axis8-F04: payment_method.attached must restore dormant connections."""
    from app.models.connection import InstanceConnection
    from sqlalchemy import select as sa_select

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)

    async with tenant_session(admin) as s:
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="crm",
                auth_class="oauth_token",
                provider="salesforce",
                status="dormant",
            )
        )
        await s.flush()

    event = StripeEvent(
        event_id="evt_pm_att_f04",
        type="payment_method.attached",
        data={"card": {"brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2028}},
    )
    async with tenant_session(admin) as s:
        await billing_webhooks.handle_event(s, admin_id=admin, event=event)

    async with tenant_session(admin) as s:
        conn = (
            await s.execute(
                sa_select(InstanceConnection).where(
                    InstanceConnection.admin_id == admin,
                    InstanceConnection.connection_type == "crm",
                )
            )
        ).scalar_one()
        assert conn.status == "unconfigured", (
            f"Dormant connection must be restored on pm.attached; got {conn.status!r}"
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_late_invoice_never_moves_the_anchor_backwards() -> None:
    """The counter rolls forward on its own (F030); an invoice for a period the anchor has
    already passed must not re-open it and hand back a spent free 50."""
    admin = uuid.uuid4()
    await _seed_tenant(admin)
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        acct.period_start = datetime(2030, 3, 1, tzinfo=UTC)
        acct.period_resets_at = datetime(2030, 4, 1, tzinfo=UTC)
        await s.flush()
    event = StripeEvent(
        event_id="evt_late",
        type="invoice.paid",
        data={
            "period_start": int(datetime(2030, 1, 1, tzinfo=UTC).timestamp()),
            "period_end": int(datetime(2030, 2, 1, tzinfo=UTC).timestamp()),
        },
    )
    async with tenant_session(admin) as s:
        await billing_webhooks.handle_event(s, admin_id=admin, event=event)
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        assert acct.period_start == datetime(2030, 3, 1, tzinfo=UTC)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_late_invoice_leaves_the_period_end_alone() -> None:
    """An invoice that does not advance the anchor changes nothing about the period. It
    used to rewrite the end as add_months(anchor, 1), which for a Jan-31 anchor's Feb-28
    period replaced Stripe's Mar 31 with Mar 28. The audit row does not claim the period
    advanced either."""
    from app.models.audit import AdminAuditLog
    from sqlalchemy import select as sa_select

    admin = uuid.uuid4()
    await _seed_tenant(admin)
    feb28, mar31 = datetime(2030, 2, 28, tzinfo=UTC), datetime(2030, 3, 31, tzinfo=UTC)
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        acct.period_start = feb28
        acct.period_resets_at = mar31
        await s.flush()
    event = StripeEvent(
        event_id="evt_late_month_end",
        type="invoice.paid",
        data={
            "billing_reason": "subscription_cycle",
            "period_start": int(datetime(2029, 12, 31, tzinfo=UTC).timestamp()),
            "period_end": int(datetime(2030, 1, 31, tzinfo=UTC).timestamp()),
        },
    )
    async with tenant_session(admin) as s:
        assert await billing_webhooks.handle_event(s, admin_id=admin, event=event) == (
            "invoice_paid"
        )
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        assert (acct.period_start, acct.period_resets_at) == (feb28, mar31)
        payload = (
            await s.execute(
                sa_select(AdminAuditLog.event_payload).where(
                    AdminAuditLog.admin_id == admin,
                    AdminAuditLog.event_type == "invoice_paid",
                )
            )
        ).scalar_one()
        assert (payload["reset"], payload["reason"]) == ("none", "period_already_current")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_pm_attached_clears_dunning_and_makes_the_card_the_invoice_default() -> None:
    """A new card is the remedy for a failing one (F012): dunning clears on attach instead
    of holding the account in reduced service until an invoice happens to succeed. And the
    card becomes the customer's invoice default (F006) — Checkout in setup mode does not
    do that by itself, so the first metered invoice had nothing to charge."""
    from app.billing.stripe_gateway import FakeStripeGateway, set_stripe_gateway
    from app.models.audit import AdminAuditLog
    from sqlalchemy import select as sa_select

    admin = uuid.uuid4()
    await _seed_tenant(admin)
    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    try:
        async with tenant_session(admin) as s:
            acct = await metering.get_or_create_billing_account(s, admin_id=admin)
            acct.stripe_customer_id = "cus_dunned"
            acct.dunning_state = dunning.REDUCED
            acct.dunning_started_at = datetime(2026, 6, 1, tzinfo=UTC)
            await s.flush()
        event = StripeEvent(
            event_id="evt_new_card",
            type="payment_method.attached",
            data={
                "id": "pm_new",
                "customer": "cus_dunned",
                "card": {"brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2030},
            },
        )
        async with tenant_session(admin) as s:
            await billing_webhooks.handle_event(s, admin_id=admin, event=event)
        async with tenant_session(admin) as s:
            acct = await metering.get_or_create_billing_account(s, admin_id=admin)
            assert acct.dunning_state == dunning.NONE
            assert acct.dunning_started_at is None
            assert acct.billing_state == "payg_enabled"
            rows = (
                (
                    await s.execute(
                        sa_select(AdminAuditLog).where(
                            AdminAuditLog.admin_id == admin,
                            AdminAuditLog.event_type == "dunning_state_changed",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert [r.event_payload["reason"] for r in rows] == ["payment_method_attached"]
        assert gw.default_payment_methods == {"cus_dunned": "pm_new"}
    finally:
        set_stripe_gateway(None)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_payment_failed_tells_the_owner_stripes_attempt_count() -> None:
    """The retry email names Stripe's own attempt number (F027), not a number derived from
    elapsed days that said "attempt 1" for every retry inside the first day."""
    from app.models.audit import AdminAuditLog
    from sqlalchemy import select as sa_select

    admin = uuid.uuid4()
    await _seed_tenant(admin)
    now = datetime(2026, 6, 20, tzinfo=UTC)
    event = StripeEvent(
        event_id="evt_fail_3", type="invoice.payment_failed", data={"attempt_count": 3}
    )
    async with tenant_session(admin) as s:
        await billing_webhooks.handle_event(s, admin_id=admin, event=event, now=now)
    async with tenant_session(admin) as s:
        notice = (
            await s.execute(
                sa_select(AdminAuditLog).where(
                    AdminAuditLog.admin_id == admin,
                    AdminAuditLog.event_type == "lifecycle_notice_sent",
                )
            )
        ).scalar_one()
        assert notice.event_payload.get("attempt_number") == 3


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_dormant_connections_restore_to_the_status_they_had_before(dormant_rows) -> None:
    """Legal §A3 restores a dormant connection; F069: to what it WAS, not to a blanket
    ``unconfigured`` that told the owner to reconnect a calendar whose grant never changed."""
    admin, statuses_before = dormant_rows
    event = StripeEvent(event_id="evt_restore", type="invoice.paid", data={})
    async with tenant_session(admin) as s:
        await billing_webhooks.handle_event(s, admin_id=admin, event=event)
    async with tenant_session(admin) as s:
        from app.models.connection import InstanceConnection
        from sqlalchemy import select as sa_select

        rows = (await s.execute(sa_select(InstanceConnection))).scalars().all()
        after = {r.connection_type: (r.status, r.status_detail) for r in rows}
    assert after == statuses_before
    for status, _ in after.values():
        assert status != "unconfigured"


@pytest.fixture
async def dormant_rows() -> tuple[uuid.UUID, dict[str, tuple[str, str | None]]]:
    """Two connected action tools with distinct details, sent dormant by the real path."""
    from app.models.connection import InstanceConnection

    admin = uuid.uuid4()
    iid = await _seed_tenant(admin)
    before: dict[str, tuple[str, str | None]] = {}
    async with tenant_session(admin) as s:
        for ctype, provider, detail in (
            ("calendar", "google", None),
            ("crm", "hubspot", "verified 2026-08-30"),
        ):
            s.add(
                InstanceConnection(
                    connection_id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    connection_type=ctype,
                    auth_class="oauth_token",
                    provider=provider,
                    status="connected",
                    status_detail=detail,
                )
            )
            before[ctype] = ("connected", detail)
        await s.flush()
        assert await dunning.set_dormant_for_action_tools(s, admin_id=admin, reason="test") == 2
    return admin, before


# --- the replay window (P0-6) ------------------------------------------------ #


@pytest.mark.usefixtures("db_engine_reset")
async def test_an_a_b_a_redelivery_does_not_reprocess_a() -> None:
    """The guard was a comparison against a single ``last_stripe_event_id`` column — a
    one-deep window, so A → B → A saw a different id in the column and re-applied A.

    Re-applying ``invoice.paid`` deletes the usage counter a second time: a PAYG tenant
    who has run conversations since the invoice loses every one of them and is never
    billed for them.
    """
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, counted=180)
    paid = StripeEvent(event_id="evt_A", type="invoice.paid", data=_settled_calendar_period())
    other = StripeEvent(event_id="evt_B", type="invoice.payment_failed", data={})

    async with tenant_session(admin) as s:
        assert await billing_webhooks.handle_event(s, admin_id=admin, event=paid) == "invoice_paid"

    # The tenant keeps using the product after the invoice was paid.
    async with tenant_session(admin) as s:
        for _ in range(180):
            await budget.count_session_once(
                s, admin_id=admin, instance_id=iid, session_id=uuid.uuid4()
            )

    async with tenant_session(admin) as s:
        await billing_webhooks.handle_event(s, admin_id=admin, event=other)
    async with tenant_session(admin) as s:
        replay = await billing_webhooks.handle_event(s, admin_id=admin, event=paid)

    assert replay == "duplicate"
    async with tenant_session(admin) as s:
        b = await metering.build_budget(s, admin_id=admin, instance_id=iid)
    assert b.conversations_this_period == 180  # not wiped by the replay


@pytest.mark.usefixtures("db_engine_reset")
async def test_a_replayed_detach_does_not_re_dormant_a_recarded_account() -> None:
    """detached → attached → replayed detached used to flip the account back to free_cap
    and re-dormant its tools, silently disabling a customer who had already re-carded."""
    admin = uuid.uuid4()
    await _seed_tenant(admin)
    detached = StripeEvent(event_id="evt_det", type="payment_method.detached", data={})
    attached = StripeEvent(
        event_id="evt_att",
        type="payment_method.attached",
        data={"card": {"brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2030}},
    )

    for event in (detached, attached):
        async with tenant_session(admin) as s:
            await billing_webhooks.handle_event(s, admin_id=admin, event=event)
    async with tenant_session(admin) as s:
        assert await billing_webhooks.handle_event(s, admin_id=admin, event=detached) == "duplicate"

    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        assert acct.billing_state == "payg_enabled"
        assert acct.pm_last4 == "4242"


@pytest.mark.usefixtures("db_engine_reset")
async def test_the_replay_gate_is_per_tenant() -> None:
    """Stripe event ids are globally unique, but the gate must not let one tenant's
    delivery swallow another's — a shared id would silently drop a real event."""
    a, b = uuid.uuid4(), uuid.uuid4()
    await _seed_tenant(a)
    await _seed_tenant(b)
    async with tenant_session(a) as s:
        first = await billing_webhooks.handle_event(
            s, admin_id=a, event=StripeEvent(event_id="evt_shared", type="invoice.paid", data={})
        )
    async with tenant_session(b) as s:
        second = await billing_webhooks.handle_event(
            s, admin_id=b, event=StripeEvent(event_id="evt_shared_b", type="invoice.paid", data={})
        )
    assert (first, second) == ("invoice_paid", "invoice_paid")
