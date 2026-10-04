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

pytestmark = [pytest.mark.asyncio]

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


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_subscription_renewed_resets_budget():
    """Axis8-F02: customer.subscription.renewed triggers same budget reset as invoice.paid."""
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, counted=30)
    now = datetime(2026, 6, 20, tzinfo=UTC)
    ps = int(now.timestamp())
    pe = int((now.replace(month=7)).timestamp())
    event = StripeEvent(
        event_id="evt_sub_renewed",
        type="customer.subscription.renewed",
        data={"period_start": ps, "period_end": pe},
    )

    async with tenant_session(admin) as s:
        action = await billing_webhooks.handle_event(s, admin_id=admin, event=event, now=now)
    assert action == "invoice_paid"

    # The renewed period reads zero; the previous period's ledger row is retained.
    async with tenant_session(admin) as s:
        from sqlalchemy import func
        from sqlalchemy import select as sa_select

        b = await metering.build_budget(
            s, admin_id=admin, instance_id=iid, now=datetime(2026, 7, 25, tzinfo=UTC)
        )
        assert b.conversations_this_period == 0
        count = (
            await s.execute(
                sa_select(func.count())
                .select_from(ConversationBudget)
                .where(ConversationBudget.admin_id == admin)
            )
        ).scalar_one()
        assert count == 1, f"the settled period's ledger must be retained; got {count}"


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
