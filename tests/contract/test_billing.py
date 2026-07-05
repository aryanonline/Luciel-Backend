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


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
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


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
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


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
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
                "data": {"object": {"metadata": {"admin_id": str(admin)}}},
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
