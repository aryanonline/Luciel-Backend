"""Metering + billing-account + webhook + idempotent-reporting integration.

Deterministic FakeStripeGateway only — NO real Stripe network call (HARD
CONSTRAINT #1). Exercises build_budget across free_cap/at_cap/payg states, the
Stripe-webhook-driven transitions (invoice.paid reset, payment_failed dunning,
day-7 reduced, pm attach/detach), idempotent redelivery, and idempotent metered
reporting (no double-bill).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.billing import metering
from app.billing import webhooks as billing_webhooks
from app.billing.stripe_gateway import FakeStripeGateway, StripeEvent
from app.db.session import tenant_session
from app.models.budget import ConversationBudget
from app.models.instance import Account, AccountState, Instance, LucielState
from app.runtime.budget import current_billing_period_start

pytestmark = [pytest.mark.asyncio]


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
    admin = uuid.uuid4()
    iid = await _seed_tenant(admin, counted=120)
    async with tenant_session(admin) as s:
        await metering.get_or_create_billing_account(s, admin_id=admin)
    event = StripeEvent(
        event_id="evt_1",
        type="invoice.paid",
        data={
            "period_start": int(datetime(2026, 6, 1, tzinfo=UTC).timestamp()),
            "period_end": int(datetime(2026, 7, 1, tzinfo=UTC).timestamp()),
        },
    )
    async with tenant_session(admin) as s:
        action = await billing_webhooks.handle_event(s, admin_id=admin, event=event)
    assert action == "invoice_paid"
    async with tenant_session(admin) as s:
        b = await metering.build_budget(s, admin_id=admin, instance_id=iid)
    assert b.conversations_this_period == 0  # reset, NO rollover


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
        acct.stripe_subscription_id = "sub_test"
        acct.period_start = datetime(2026, 6, 1, tzinfo=UTC)
        await s.flush()
    # Report twice with the same (period, quantity) — the second is a dedup no-op.
    async with tenant_session(admin) as s:
        q1 = await metering.report_usage(s, admin_id=admin, instance_id=iid, gateway=gw)
    async with tenant_session(admin) as s:
        q2 = await metering.report_usage(s, admin_id=admin, instance_id=iid, gateway=gw)
    assert q1 == 2  # 2 blocks
    assert q2 == 2  # same quantity reported, but gateway deduped the key
    # Exactly one idempotency key recorded → no double-bill.
    assert len(gw._usage_keys) == 1


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
    await _seed_tenant(admin, counted=30)
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

    # Budget should be reset: no budget rows remain.
    async with tenant_session(admin) as s:
        from sqlalchemy import func
        from sqlalchemy import select as sa_select

        count = (
            await s.execute(
                sa_select(func.count())
                .select_from(ConversationBudget)
                .where(ConversationBudget.admin_id == admin)
            )
        ).scalar_one()
        assert count == 0, f"Budget rows must be reset on subscription.renewed; got {count}"


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
