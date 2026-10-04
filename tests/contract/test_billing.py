"""Contract conformance for /api/v1/billing/* + /api/v1/admin/usage/* + the Stripe
webhook (Architecture §3.4.1b, frozen schemas/billing.ts + schemas/analytics.ts).

Exact wire shapes (camelCase, freeAllowance literal 50, 204 on DELETE), the
no-card-data guarantee (no PAN-like field in any response), and the
verify-before-route webhook (unverified rejected). FakeStripeGateway only — NO
real Stripe network call.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from app.billing import metering
from app.billing.stripe_gateway import FakeStripeGateway, build_signature_header, set_stripe_gateway
from app.db.session import tenant_session
from app.models.budget import ConversationBudget
from app.models.instance import Instance, LucielState
from app.runtime.budget import current_billing_period_start

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


async def _signup_verify_login(client) -> uuid.UUID:
    from app.auth import access

    email = f"bill-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "t"},
    )
    acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    return acct.id


async def _seed_instance(admin: uuid.UUID, counted: int = 0) -> uuid.UUID:
    iid = uuid.uuid4()
    async with tenant_session(admin) as s:
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


# Tokens that must never appear in any billing response (no card data, §3.4.1b).
_FORBIDDEN_TOKENS = ("pan", "cardnumber", "card_number", "cvv", "cvc", "number")


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


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_get_billing_free_cap_shape(client) -> None:
    admin = await _signup_verify_login(client)
    await _seed_instance(admin, counted=10)
    r = await client.get("/api/v1/billing")
    assert r.status_code == 200
    body = r.json()
    # Exact frozen budget shape (camelCase).
    budget = body["budget"]
    assert budget["billingState"] == "free_cap"
    assert budget["dunningState"] == "none"
    assert budget["freeAllowance"] == 50  # z.literal(50) — serializes exactly 50
    assert budget["conversationsThisPeriod"] == 10
    assert budget["billedThisPeriod"] == 0
    assert budget["atCap"] is False
    # PAYG heads-up is always false for a free_cap account (no billed blocks).
    assert budget["nearNextBlock"] is False
    assert "periodResetsAt" in budget
    # paymentMethod present ONLY when payg_enabled.
    assert body.get("paymentMethod") is None
    # No card-data token anywhere in the response.
    blob = json.dumps(body).lower()
    assert not any(tok in blob for tok in _FORBIDDEN_TOKENS)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_get_billing_at_cap(client) -> None:
    admin = await _signup_verify_login(client)
    await _seed_instance(admin, counted=50)
    r = await client.get("/api/v1/billing")
    assert r.json()["budget"]["atCap"] is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_get_billing_payg_includes_display_payment_method(client) -> None:
    admin = await _signup_verify_login(client)
    await _seed_instance(admin, counted=240)
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        acct.billing_state = "payg_enabled"
        acct.pm_brand = "visa"
        acct.pm_last4 = "4242"
        acct.pm_exp_month = 12
        acct.pm_exp_year = 2028
        await s.flush()
    r = await client.get("/api/v1/billing")
    body = r.json()
    assert body["budget"]["billingState"] == "payg_enabled"
    assert body["budget"]["billedThisPeriod"] == 190
    pm = body["paymentMethod"]
    # Display tuple only — exactly the four frozen fields, NO PAN.
    assert pm == {"brand": "visa", "last4": "4242", "expMonth": 12, "expYear": 2028}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_payg_near_next_block_nudge(client) -> None:
    # PAYG heads-up (§3.4.1b / Vision §7): a live-card account >=80% through its
    # current billed 100-block gets nearNextBlock=True (honest nudge, not a cap).
    # counted=240 -> over=190 -> position in block = 90 -> True.
    admin = await _signup_verify_login(client)
    await _seed_instance(admin, counted=240)
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        acct.billing_state = "payg_enabled"
        acct.pm_brand = "visa"
        acct.pm_last4 = "4242"
        acct.pm_exp_month = 12
        acct.pm_exp_year = 2028
        await s.flush()
    r = await client.get("/api/v1/billing")
    assert r.json()["budget"]["nearNextBlock"] is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_payg_not_near_next_block(client) -> None:
    # Early in a block -> not near. counted=170 -> over=120 -> position 20 -> False.
    admin = await _signup_verify_login(client)
    await _seed_instance(admin, counted=170)
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        acct.billing_state = "payg_enabled"
        acct.pm_brand = "visa"
        acct.pm_last4 = "4242"
        acct.pm_exp_month = 12
        acct.pm_exp_year = 2028
        await s.flush()
    r = await client.get("/api/v1/billing")
    assert r.json()["budget"]["nearNextBlock"] is False


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset", "stripe_test_posture")
async def test_checkout_returns_url(client) -> None:
    admin = await _signup_verify_login(client)
    await _seed_instance(admin)
    set_stripe_gateway(FakeStripeGateway())
    try:
        r = await client.post("/api/v1/billing/checkout")
        assert r.status_code == 200
        url = r.json()["url"]
        assert url.startswith("https://checkout.stripe.com/")
    finally:
        set_stripe_gateway(None)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset", "stripe_test_posture")
async def test_checkout_return_urls_derive_from_app_base_url(client, monkeypatch) -> None:
    """Live-caught 2026-08-17: the return URLs were hardcoded to a placeholder
    domain the product does not serve, so after saving a card Stripe redirected
    the admin off-site. They must derive from app_base_url (the verify-link rule,
    auth.py) and land on the REAL dashboard route /dashboard/billing."""
    from app.config import get_settings

    class _RecordingGateway(FakeStripeGateway):
        def __init__(self) -> None:
            super().__init__()
            self.checkout_urls: list[tuple[str, str]] = []

        async def create_checkout_session(self, *, customer_id, success_url, cancel_url):
            self.checkout_urls.append((success_url, cancel_url))
            return await super().create_checkout_session(
                customer_id=customer_id, success_url=success_url, cancel_url=cancel_url
            )

    admin = await _signup_verify_login(client)
    await _seed_instance(admin)
    # Trailing slash on purpose: the derivation must not compose a double slash.
    monkeypatch.setattr(get_settings(), "app_base_url", "https://www.vantagemind.ai/")
    gateway = _RecordingGateway()
    set_stripe_gateway(gateway)
    try:
        r = await client.post("/api/v1/billing/checkout")
        assert r.status_code == 200
        ((success, cancel),) = gateway.checkout_urls
        assert success == "https://www.vantagemind.ai/dashboard/billing?checkout=success"
        assert cancel == "https://www.vantagemind.ai/dashboard/billing?checkout=cancel"
    finally:
        set_stripe_gateway(None)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_remove_payment_method_returns_billing_info_and_reverts(client) -> None:
    # Axis10-F01: removePaymentMethod returns 200 + the updated BillingInfo (matching
    # the frontend `Promise<BillingInfo>` and the mock adapter), not a bare 204.
    admin = await _signup_verify_login(client)
    await _seed_instance(admin, counted=60)
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        acct.billing_state = "payg_enabled"
        acct.stripe_customer_id = "cus_test"
        acct.pm_brand = "visa"
        acct.pm_last4 = "4242"
        await s.flush()
    set_stripe_gateway(FakeStripeGateway())
    try:
        r = await client.delete("/api/v1/billing/payment-method")
        assert r.status_code == 200
        body = r.json()
        # The response body IS the updated BillingInfo: reverted to free_cap, no card.
        assert body["budget"]["billingState"] == "free_cap"
        assert body["budget"]["atCap"] is True
        assert body.get("paymentMethod") in (None, {})
    finally:
        set_stripe_gateway(None)
    # And a subsequent GET agrees.
    g = await client.get("/api/v1/billing")
    assert g.json()["budget"]["billingState"] == "free_cap"
    assert g.json()["budget"]["atCap"] is True
    assert g.json().get("paymentMethod") is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_remove_payment_method_settles_the_residual_before_detaching(client) -> None:
    """Audit F031: the admin's own card removal is a loss of PAYG exactly like a
    Stripe-side detach, so it flushes the accrued residual and ends the metered
    subscription with a final invoice BEFORE the card goes — not after, when there is
    nothing left to charge."""
    admin = await _signup_verify_login(client)
    await _seed_instance(admin, counted=210)
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        acct.billing_state = "payg_enabled"
        acct.stripe_customer_id = "cus_settle"
        acct.stripe_subscription_id = "sub_settle"
        acct.accrued_blocks = 2
        acct.last_reported_blocks = 1
        acct.pm_brand = "visa"
        acct.pm_last4 = "4242"
        await s.flush()
    gw = FakeStripeGateway()
    gw.seed_customer("cus_settle", subscription_id="sub_settle")
    set_stripe_gateway(gw)
    try:
        r = await client.delete("/api/v1/billing/payment-method")
        assert r.status_code == 200
    finally:
        set_stripe_gateway(None)
    assert [e.value for e in gw.meter_events] == [1], "the owed block was billed first"
    assert gw.cancelled_subscriptions == [("sub_settle", True)]
    assert gw.default_payment_methods == {}
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        assert acct.stripe_subscription_id is None
        assert acct.billing_state == "free_cap"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_billing_read_says_when_payments_are_not_live(client, monkeypatch) -> None:
    """Audit F026: in the mock Stripe posture the checkout URL goes nowhere, so the read
    carries ``paymentsAvailable`` and the dashboard can say so instead of offering a
    button that pretends to work."""
    from app.config import get_settings

    admin = await _signup_verify_login(client)
    await _seed_instance(admin, counted=0)
    monkeypatch.setattr(get_settings(), "stripe_integration_posture", "mock")
    assert (await client.get("/api/v1/billing")).json()["paymentsAvailable"] is False
    monkeypatch.setattr(get_settings(), "stripe_integration_posture", "test")
    assert (await client.get("/api/v1/billing")).json()["paymentsAvailable"] is True


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_usage_overview_shape(client) -> None:
    admin = await _signup_verify_login(client)
    await _seed_instance(admin, counted=12)
    r = await client.get("/api/v1/admin/usage/overview")
    assert r.status_code == 200
    body = r.json()
    for key in (
        "conversationsThisPeriod",
        "conversationsTotal",
        "leadsThisPeriod",
        "appointmentsBooked",
        "responseTimeP50Seconds",
        "responseTimeP95Seconds",
        "escalationsBySignal",
        "channelMix",
        "budgetUtilization",
        "busiestTimes",
    ):
        assert key in body, f"missing {key}"
    assert body["conversationsThisPeriod"] == 12


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_usage_audit_log_shape(client) -> None:
    admin = await _signup_verify_login(client)
    await _seed_instance(admin)
    r = await client.get("/api/v1/admin/usage/audit-log")
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    if rows:  # the signup/verify path emits audit rows
        row = rows[0]
        assert "eventId" in row and "eventType" in row and "at" in row


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset", "stripe_test_posture")
async def test_webhook_unverified_rejected(client) -> None:
    set_stripe_gateway(FakeStripeGateway())
    try:
        body = json.dumps({"id": "evt_1", "type": "invoice.paid", "data": {"object": {}}}).encode()
        # No / bad signature → rejected, nothing processed (verify-before-route).
        r = await client.post(
            "/api/v1/webhooks/stripe", content=body, headers={"Stripe-Signature": "t=1,v1=bad"}
        )
        assert r.status_code == 422
    finally:
        set_stripe_gateway(None)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset", "stripe_test_posture")
async def test_webhook_verified_invoice_paid_processed(client) -> None:
    admin = await _signup_verify_login(client)
    await _seed_instance(admin, counted=120)
    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    try:
        payload = json.dumps(
            {
                "id": "evt_paid",
                "type": "invoice.paid",
                "data": {
                    "object": {"metadata": {"admin_id": str(admin)}, **_settled_calendar_period()}
                },
            }
        ).encode()
        header = build_signature_header(gw.WEBHOOK_SECRET, payload)
        r = await client.post(
            "/api/v1/webhooks/stripe", content=payload, headers={"Stripe-Signature": header}
        )
        assert r.status_code == 200
        assert r.json()["action"] == "invoice_paid"
    finally:
        set_stripe_gateway(None)
    # Counter reset, no rollover.
    g = await client.get("/api/v1/billing")
    assert g.json()["budget"]["conversationsThisPeriod"] == 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_budget_reports_the_ledger_when_the_card_is_gone(client) -> None:
    """Round 6 WP-I (audit F025): a dunned account that ran a paid block while its card
    was live used to read billedThisPeriod 0 — as if nothing had ever been billed. The
    wire now carries the ledger: not billable for NEW usage, at cap, but the block it
    ran is still shown, with how much of it Stripe has already been told about."""
    admin = await _signup_verify_login(client)
    await _seed_instance(admin, counted=70)
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        acct.billing_state = "payg_enabled"
        acct.dunning_state = "reduced"
        acct.accrued_blocks = 1
        acct.last_reported_blocks = 1
        acct.accrual_period_start = current_billing_period_start()
    r = await client.get("/api/v1/billing")
    assert r.status_code == 200
    b = r.json()["budget"]
    assert b["billable"] is False and b["atCap"] is True
    assert b["billedThisPeriod"] == 20  # 70 counted − 50 free, inside the one accrued block
    assert b["accruedBlocks"] == 1 and b["reportedBlocks"] == 1

    # A live card: the same figures read billable, with the count above free as before.
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        acct.dunning_state = "none"
    b = (await client.get("/api/v1/billing")).json()["budget"]
    assert b["billable"] is True and b["atCap"] is False and b["billedThisPeriod"] == 20

    # A free-cap account that never accrued anything shows zero, not a phantom charge.
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        acct.billing_state = "free_cap"
        acct.accrued_blocks = 0
        acct.last_reported_blocks = 0
    b = (await client.get("/api/v1/billing")).json()["budget"]
    assert b["billedThisPeriod"] == 0 and b["accruedBlocks"] == 0 and b["billable"] is False
