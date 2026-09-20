"""Money and account-management truth at the billing routes (round 7 WP-1 #5).

  * POST /api/v1/billing/portal opens the Stripe Billing Portal — the card-UPDATE path
    (Checkout only adds a first card) — for the tenant's own customer, lands back on
    the dashboard billing page, and is audited.
  * The mock posture refuses checkout and the portal (503) instead of minting a URL
    that goes nowhere, and the webhook receiver ignores VERIFIED events (200, logged,
    no state change) so a real signing secret in a mock-posture deployment cannot
    mutate billing — while an unverified event is still rejected first.
  * GET /api/v1/billing reconciles a card Stripe already holds while the
    ``payment_method.attached`` webhook is still in flight: the same transition the
    webhook applies, applied once, so "card saved" and "no card on file" never stand
    side by side on the dashboard.

FakeStripeGateway only — NO real Stripe network call anywhere in this file.
"""

from __future__ import annotations

import json
import logging
import uuid

import pytest
from app.billing import metering
from app.billing.stripe_gateway import (
    FakeStripeGateway,
    PaymentMethodDisplay,
    build_signature_header,
    set_stripe_gateway,
)
from app.config import get_settings
from app.db.session import tenant_session
from app.models.audit import AdminAuditLog
from app.models.billing import ProcessedStripeEvent
from app.models.budget import ConversationBudget
from app.models.instance import Instance, LucielState
from app.runtime.budget import current_billing_period_start
from sqlalchemy import select

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


async def _signup_verify_login(client) -> uuid.UUID:
    from app.auth import access

    email = f"portal-{uuid.uuid4().hex[:8]}@x.com"
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


async def _bind_customer(admin: uuid.UUID, gw: FakeStripeGateway, *, payg: bool) -> str:
    """Give the tenant a Stripe customer the fake knows; optionally a card on file."""
    cid = await gw.create_customer(admin_id=admin, email=f"{admin}@x.com")
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        acct.stripe_customer_id = cid
        if payg:
            acct.billing_state = "payg_enabled"
            acct.pm_brand, acct.pm_last4 = "visa", "4242"
            acct.pm_exp_month, acct.pm_exp_year = 12, 2030
        await s.flush()
    return cid


async def _audit(admin: uuid.UUID, event_type: str) -> list[dict[str, object]]:
    async with tenant_session(admin) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog)
                    .where(AdminAuditLog.admin_id == admin, AdminAuditLog.event_type == event_type)
                    .order_by(AdminAuditLog.seq.asc())
                )
            )
            .scalars()
            .all()
        )
    return [dict(r.event_payload) for r in rows]


async def _billing_row(admin: uuid.UUID) -> tuple[str, str | None, str | None]:
    async with tenant_session(admin) as s:
        acct = await metering.get_or_create_billing_account(s, admin_id=admin)
        return acct.billing_state, acct.stripe_customer_id, acct.pm_last4


def _signed(gw: FakeStripeGateway, event: dict) -> tuple[bytes, dict[str, str]]:
    payload = json.dumps(event).encode()
    return payload, {"Stripe-Signature": build_signature_header(gw.WEBHOOK_SECRET, payload)}


# --- the portal: the card-UPDATE path ----------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset", "stripe_test_posture")
async def test_portal_opens_the_card_update_page_for_the_tenants_customer(
    client, monkeypatch
) -> None:
    admin = await _signup_verify_login(client)
    await _seed_instance(admin)
    # Trailing slash on purpose: the return URL must not compose a double slash.
    monkeypatch.setattr(get_settings(), "app_base_url", "https://www.vantagemind.ai/")
    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    try:
        cid = await _bind_customer(admin, gw, payg=True)
        r = await client.post("/api/v1/billing/portal")
        assert r.status_code == 200
        assert r.json() == {
            "url": f"https://billing.stripe.com/p/session/bps_fake_{admin.hex[:16]}"
        }
    finally:
        set_stripe_gateway(None)
    # The right customer, sent back to the REAL dashboard billing route.
    assert gw.portal_sessions == [(cid, "https://www.vantagemind.ai/dashboard/billing")]
    # Audited by name, session id only (no card data ever rides an audit payload).
    assert await _audit(admin, "billing_portal_session_created") == [
        {"portal_session_id": f"bps_fake_{admin.hex[:16]}"}
    ]
    # Opening the portal changes nothing on our side: the card change, if any, comes
    # back as a payment_method webhook exactly like a Checkout card.
    assert await _billing_row(admin) == ("payg_enabled", cid, "4242")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset", "stripe_test_posture")
async def test_portal_refuses_a_tenant_with_no_customer_to_manage(client) -> None:
    """No Checkout ever happened → no Stripe customer → nothing for the portal to edit.
    A typed 409, not a silently minted customer: adding a first card is Checkout's job."""
    admin = await _signup_verify_login(client)
    await _seed_instance(admin)
    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    try:
        r = await client.post("/api/v1/billing/portal")
    finally:
        set_stripe_gateway(None)
    assert r.status_code == 409
    assert "add a card first" in r.json()["message"]
    assert gw.portal_sessions == []
    assert await _audit(admin, "billing_portal_session_created") == []
    assert (await _billing_row(admin))[1] is None  # no customer was created as a side effect


# --- the mock posture: billing is not live, and the routes say so ------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_mock_posture_refuses_checkout_and_portal_instead_of_dead_urls(
    client, monkeypatch
) -> None:
    """The fake's Checkout URL goes nowhere. A 200 carrying it was a lie the dashboard
    could only detect from ``paymentsAvailable``; now both card flows refuse with a
    typed 503 BEFORE any side effect."""
    monkeypatch.setattr(get_settings(), "stripe_integration_posture", "mock")
    admin = await _signup_verify_login(client)
    await _seed_instance(admin)
    set_stripe_gateway(FakeStripeGateway())
    try:
        assert (await client.get("/api/v1/billing")).json()["paymentsAvailable"] is False
        r = await client.post("/api/v1/billing/checkout")
        assert r.status_code == 503
        assert "not live" in r.json()["message"]
        r = await client.post("/api/v1/billing/portal")
        assert r.status_code == 503
        assert "not live" in r.json()["message"]
    finally:
        set_stripe_gateway(None)
    # No customer minted, nothing audited, no URL left the deployment.
    assert await _billing_row(admin) == ("free_cap", None, None)
    assert await _audit(admin, "billing_checkout_started") == []
    assert await _audit(admin, "billing_portal_session_created") == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_mock_posture_webhook_is_verified_then_ignored_without_state_change(
    client, monkeypatch, caplog
) -> None:
    """A real whsec_ provisioned into a mock-posture deployment must not let Stripe's
    account state (or a captured replay of it) mutate billing the deployment cannot
    honour. Verify-before-route still stands: an unverified event is rejected first.
    Nothing is claimed, so the same event is processed once the posture is real."""
    monkeypatch.setattr(get_settings(), "stripe_integration_posture", "mock")
    admin = await _signup_verify_login(client)
    await _seed_instance(admin, counted=120)
    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    try:
        event = {
            "id": "evt_mock_paid",
            "type": "invoice.paid",
            "data": {"object": {"metadata": {"admin_id": str(admin)}}},
        }
        payload, headers = _signed(gw, event)
        # Unverified → 422, in every posture (HARD CONSTRAINT #4 is not weakened).
        bad = await client.post(
            "/api/v1/webhooks/stripe", content=payload, headers={"Stripe-Signature": "t=1,v1=bad"}
        )
        assert bad.status_code == 422

        with caplog.at_level(logging.WARNING, logger="app.api.v1.webhooks.stripe"):
            r = await client.post("/api/v1/webhooks/stripe", content=payload, headers=headers)
        assert r.status_code == 200  # Stripe stops retrying
        assert r.json() == {"status": "ignored", "reason": "mock_posture"}
        assert any(
            "ignored" in rec.getMessage() and "evt_mock_paid" in rec.getMessage()
            for rec in caplog.records
            if rec.levelno == logging.WARNING
        )
        # No state change: the counter was NOT reset, nothing was audited or claimed.
        assert (await client.get("/api/v1/billing")).json()["budget"][
            "conversationsThisPeriod"
        ] == 120
        assert await _audit(admin, "invoice_paid") == []
        async with tenant_session(admin) as s:
            claimed = (
                await s.execute(
                    select(ProcessedStripeEvent).where(
                        ProcessedStripeEvent.event_id == "evt_mock_paid"
                    )
                )
            ).scalar_one_or_none()
        assert claimed is None

        # The posture becomes real: Stripe's redelivery of the SAME event now applies.
        monkeypatch.setattr(get_settings(), "stripe_integration_posture", "test")
        r = await client.post("/api/v1/webhooks/stripe", content=payload, headers=headers)
        assert r.status_code == 200
        assert r.json() == {"status": "ok", "action": "invoice_paid"}
        assert (await client.get("/api/v1/billing")).json()["budget"][
            "conversationsThisPeriod"
        ] == 0
    finally:
        set_stripe_gateway(None)


# --- the billing read reconciles a card Stripe holds -------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset", "stripe_test_posture")
async def test_billing_read_reconciles_a_card_stripe_holds_before_the_webhook_lands(
    client,
) -> None:
    """Checkout completed and redirected the admin back to the dashboard; the
    payment_method.attached webhook is still in flight. The read used to say "no card
    on file" (free_cap) beside the return URL's "card saved". Now it applies the same
    transition the webhook applies — once — and the late webhook finds nothing left to
    change except the invoice default only it can set."""
    admin = await _signup_verify_login(client)
    await _seed_instance(admin, counted=60)
    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    try:
        cid = await _bind_customer(admin, gw, payg=False)
        # A customer with NO card yet is left alone: still free_cap, at the cap.
        b = (await client.get("/api/v1/billing")).json()
        assert b["budget"]["billingState"] == "free_cap" and b["budget"]["atCap"] is True
        assert b.get("paymentMethod") is None

        # Checkout completes on Stripe's side (the fake attaches the card the way a
        # completed setup-mode session does); no webhook has been delivered.
        await gw.create_checkout_session(
            customer_id=cid, success_url="https://s", cancel_url="https://c"
        )
        b = (await client.get("/api/v1/billing")).json()
        assert b["budget"]["billingState"] == "payg_enabled"
        assert b["budget"]["atCap"] is False and b["budget"]["billable"] is True
        assert b["paymentMethod"] == {
            "brand": "visa",
            "last4": "4242",
            "expMonth": 12,
            "expYear": 2030,
        }
        assert await _audit(admin, "lifecycle_transition") == [
            {"from": "free_cap", "to": "payg_enabled", "trigger": "payment_method"}
        ]
        assert await _audit(admin, "payment_method_added") == [
            {
                "brand": "visa",
                "last4": "4242",
                "invoice_default_set": None,
                "source": "billing_read",
            }
        ]

        # Idempotent: a second read re-applies nothing.
        b = (await client.get("/api/v1/billing")).json()
        assert b["budget"]["billingState"] == "payg_enabled"
        assert len(await _audit(admin, "lifecycle_transition")) == 1
        assert len(await _audit(admin, "payment_method_added")) == 1

        # The delayed webhook lands: same state, no second transition, and the card is
        # made the invoice default (F006) — the one thing the read could not do.
        payload, headers = _signed(
            gw,
            {
                "id": "evt_late_attach",
                "type": "payment_method.attached",
                "data": {
                    "object": {
                        "id": "pm_late",
                        "customer": cid,
                        "card": {
                            "brand": "visa",
                            "last4": "4242",
                            "exp_month": 12,
                            "exp_year": 2030,
                        },
                    }
                },
            },
        )
        r = await client.post("/api/v1/webhooks/stripe", content=payload, headers=headers)
        assert r.status_code == 200 and r.json()["action"] == "payment_method.attached"
        assert gw.default_payment_methods == {cid: "pm_late"}
        assert len(await _audit(admin, "lifecycle_transition")) == 1
        added = await _audit(admin, "payment_method_added")
        assert [a["source"] for a in added] == ["billing_read", "payment_method.attached"]
        assert added[1]["invoice_default_set"] is True
        b = (await client.get("/api/v1/billing")).json()
        assert b["budget"]["billingState"] == "payg_enabled"
    finally:
        set_stripe_gateway(None)
    assert await _billing_row(admin) == ("payg_enabled", cid, "4242")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_billing_read_never_reconciles_in_the_mock_posture(client, monkeypatch) -> None:
    """The fake is not Stripe: what it "holds" must never flip a real billing row."""
    monkeypatch.setattr(get_settings(), "stripe_integration_posture", "mock")
    admin = await _signup_verify_login(client)
    await _seed_instance(admin, counted=60)
    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    try:
        cid = await _bind_customer(admin, gw, payg=False)
        await gw.create_checkout_session(
            customer_id=cid, success_url="https://s", cancel_url="https://c"
        )
        b = (await client.get("/api/v1/billing")).json()
    finally:
        set_stripe_gateway(None)
    assert b["budget"]["billingState"] == "free_cap" and b.get("paymentMethod") is None
    assert await _audit(admin, "lifecycle_transition") == []
    assert await _billing_row(admin) == ("free_cap", cid, None)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset", "stripe_test_posture")
async def test_a_stripe_read_failure_does_not_break_the_billing_read(client, caplog) -> None:
    """The reconcile is a courtesy to the dashboard, not a dependency of the read: a
    Stripe blip is logged and the read stays honest about what WE know (free_cap)."""

    class _DownGateway(FakeStripeGateway):
        async def get_payment_method(self, *, customer_id: str) -> PaymentMethodDisplay | None:
            raise RuntimeError("stripe is down")

    admin = await _signup_verify_login(client)
    await _seed_instance(admin, counted=10)
    gw = _DownGateway()
    set_stripe_gateway(gw)
    try:
        cid = await _bind_customer(admin, gw, payg=False)
        with caplog.at_level(logging.ERROR, logger="app.api.v1.billing"):
            r = await client.get("/api/v1/billing")
    finally:
        set_stripe_gateway(None)
    assert r.status_code == 200
    assert r.json()["budget"]["billingState"] == "free_cap"
    assert any("reconcile read failed" in rec.getMessage() for rec in caplog.records)
    assert await _billing_row(admin) == ("free_cap", cid, None)
