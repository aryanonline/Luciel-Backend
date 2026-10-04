"""Confirms PAYG math (billedThisPeriod / blocks / cents, the locked $39-per-100
numbers — Vision §7 / §3.4.1b) together with the period-reset behavior, both
DRIVEN BY SIGNED Stripe webhooks (verify-before-route, HARD CONSTRAINT #4).

FakeStripeGateway only — every webhook is HMAC-signed with its WEBHOOK_SECRET.
NO real Stripe network call anywhere in this file.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
from app.billing import metering
from app.billing.metering import get_or_create_billing_account
from app.billing.stripe_gateway import FakeStripeGateway, build_signature_header, set_stripe_gateway
from app.config import get_settings
from app.db.session import tenant_session
from app.models.budget import ConversationBudget
from app.models.instance import Instance, LucielState
from app.runtime.budget import current_billing_period_start

# Round 7 WP-1 #5: the webhook receiver ignores events in the mock posture, so every
# signed-webhook test here runs in the "test" posture with the fake injected.
pytestmark = [
    pytest.mark.contract,
    pytest.mark.asyncio,
    pytest.mark.usefixtures("stripe_test_posture"),
]


async def _signup_verify_login(client) -> uuid.UUID:
    from app.auth import access

    email = f"payg-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "t"},
    )
    acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    return acct.id


async def _seed_instance(admin: uuid.UUID, counted: int) -> uuid.UUID:
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


def _signed_webhook(
    gw: FakeStripeGateway,
    *,
    event_id: str,
    event_type: str,
    admin: uuid.UUID,
    extra: dict | None = None,
) -> bytes:
    return json.dumps(
        {
            "id": event_id,
            "type": event_type,
            "data": {"object": {"metadata": {"admin_id": str(admin)}, **(extra or {})}},
        }
    ).encode()


async def _make_payg_enabled(admin: uuid.UUID) -> None:
    """billedThisPeriod is only nonzero with a live card (metering.build_budget) —
    flip billing_state directly (equivalent to a prior payment_method.attached)."""
    async with tenant_session(admin) as s:
        acct = await get_or_create_billing_account(s, admin_id=admin)
        acct.billing_state = "payg_enabled"
        await s.flush()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_payg_math_at_151_conversations_matches_locked_numbers(client) -> None:
    """151 counted → 101 billed-over, ceil(101/100)=2 blocks, 2*3900=7800¢ — the
    locked boundary case (50→$0, 51→$39, 150→$39, 151→$78, Vision §7)."""
    settings = get_settings()
    admin = await _signup_verify_login(client)
    await _seed_instance(admin, counted=151)
    await _make_payg_enabled(admin)

    g = await client.get("/api/v1/billing")
    assert g.status_code == 200
    budget = g.json()["budget"]
    assert budget["conversationsThisPeriod"] == 151
    assert budget["billedThisPeriod"] == metering.billed_conversations(151, settings.free_allowance)
    assert budget["billedThisPeriod"] == 101

    # Money side isn't on the wire Budget shape — assert directly via the pure
    # metering function using the same locked constants the wire value came from.
    assert metering.billed_blocks(151, settings.free_allowance, settings.payg_block_size) == 2
    assert (
        metering.billed_cents(
            151,
            settings.free_allowance,
            settings.payg_block_size,
            settings.payg_price_per_block_cents,
        )
        == 7800
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_signed_invoice_paid_webhook_resets_budget_no_rollover(client) -> None:
    admin = await _signup_verify_login(client)
    iid = await _seed_instance(admin, counted=240)  # well above free_allowance=50
    await _make_payg_enabled(admin)

    g = await client.get("/api/v1/billing")
    assert g.json()["budget"]["conversationsThisPeriod"] == 240
    assert g.json()["budget"]["billedThisPeriod"] == 190

    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    try:
        # A real invoice names the period it settled; the next period starts at its end.
        payload = _signed_webhook(
            gw,
            event_id="evt_payg_paid",
            event_type="invoice.paid",
            admin=admin,
            extra={
                "period_start": int(datetime(2030, 1, 15, tzinfo=UTC).timestamp()),
                "period_end": int(datetime(2030, 2, 15, tzinfo=UTC).timestamp()),
            },
        )
        header = build_signature_header(gw.WEBHOOK_SECRET, payload)
        r = await client.post(
            "/api/v1/webhooks/stripe", content=payload, headers={"Stripe-Signature": header}
        )
        assert r.status_code == 200
        assert r.json()["action"] == "invoice_paid"
    finally:
        set_stripe_gateway(None)

    async with tenant_session(admin) as s:
        # The dashboard read is "now"-relative; assert the new period through the same
        # metering read at an instant inside it.
        b = await metering.build_budget(
            s, admin_id=admin, instance_id=iid, now=datetime(2030, 2, 20, tzinfo=UTC)
        )
    # Reset to zero, NO rollover of the prior 240 (§3.4.1b).
    assert b.conversations_this_period == 0
    assert b.billed_this_period == 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_unsigned_webhook_cannot_reset_budget(client) -> None:
    """Verify-before-route: an unsigned/garbled Stripe-Signature must be rejected
    BEFORE the reset logic runs — the budget must be untouched."""
    admin = await _signup_verify_login(client)
    await _seed_instance(admin, counted=240)

    set_stripe_gateway(FakeStripeGateway())
    try:
        payload = json.dumps(
            {
                "id": "evt_bad",
                "type": "invoice.paid",
                "data": {"object": {"metadata": {"admin_id": str(admin)}}},
            }
        ).encode()
        r = await client.post(
            "/api/v1/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": "t=1,v1=deadbeef"},
        )
        assert r.status_code == 422
    finally:
        set_stripe_gateway(None)

    g = await client.get("/api/v1/billing")
    assert g.json()["budget"]["conversationsThisPeriod"] == 240  # untouched


# --- tenant resolution (P1-11) ----------------------------------------------- #


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


def _customer_webhook(
    *, event_id: str, event_type: str, customer: str, period: dict[str, int] | None = None
) -> bytes:
    """A Stripe-generated event: it carries ``customer`` and NO metadata, which is
    exactly the shape the route used to answer "ignored" to. ``period`` adds the
    invoice's period_start / period_end, as every real Stripe invoice carries."""
    obj: dict[str, object] = {"customer": customer, **(period or {})}
    return json.dumps({"id": event_id, "type": event_type, "data": {"object": obj}}).encode()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_stripe_generated_invoice_paid_resolves_its_tenant_by_customer_id(client) -> None:
    """invoice.paid is generated by Stripe, so it carries no metadata.admin_id. Resolving
    on metadata alone made the route answer 200 "ignored" — the counter never reset and
    the tenant kept accruing against a period Stripe had already invoiced.
    """
    admin = await _signup_verify_login(client)
    await _seed_instance(admin, counted=240)
    async with tenant_session(admin) as s:
        acct = await get_or_create_billing_account(s, admin_id=admin)
        acct.billing_state = "payg_enabled"
        acct.stripe_customer_id = "cus_resolve_me"
        await s.flush()

    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    try:
        payload = _customer_webhook(
            event_id="evt_by_customer",
            event_type="invoice.paid",
            customer="cus_resolve_me",
            period=_settled_calendar_period(),
        )
        r = await client.post(
            "/api/v1/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": build_signature_header(gw.WEBHOOK_SECRET, payload)},
        )
    finally:
        set_stripe_gateway(None)

    assert r.status_code == 200
    assert r.json() == {"status": "ok", "action": "invoice_paid"}
    g = await client.get("/api/v1/billing")
    assert g.json()["budget"]["conversationsThisPeriod"] == 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_unknown_customer_is_ignored_not_applied_to_someone_else(client) -> None:
    """A customer id we have no binding for must resolve to nobody. Falling through to
    any tenant would apply another account's invoice to this one."""
    admin = await _signup_verify_login(client)
    await _seed_instance(admin, counted=240)
    await _make_payg_enabled(admin)

    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    try:
        payload = _customer_webhook(
            event_id="evt_unknown_cus", event_type="invoice.paid", customer="cus_not_ours"
        )
        r = await client.post(
            "/api/v1/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": build_signature_header(gw.WEBHOOK_SECRET, payload)},
        )
    finally:
        set_stripe_gateway(None)

    assert r.json() == {"status": "ignored", "reason": "unknown_tenant"}
    g = await client.get("/api/v1/billing")
    assert g.json()["budget"]["conversationsThisPeriod"] == 240  # untouched


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_customer_binding_wins_over_metadata_from_another_tenant(client) -> None:
    """metadata is attacker-influenceable on objects Stripe lets a customer create; the
    customer binding is ours. When both are present the binding must decide, or a
    forged admin_id would steer a real invoice at someone else's account."""
    victim = await _signup_verify_login(client)
    await _seed_instance(victim, counted=240)
    await _make_payg_enabled(victim)

    owner = uuid.uuid4()
    async with tenant_session(owner) as s:
        acct = await get_or_create_billing_account(s, admin_id=owner)
        acct.stripe_customer_id = "cus_owner"
        await s.flush()

    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    try:
        payload = json.dumps(
            {
                "id": "evt_conflict",
                "type": "invoice.paid",
                "data": {
                    "object": {"customer": "cus_owner", "metadata": {"admin_id": str(victim)}}
                },
            }
        ).encode()
        await client.post(
            "/api/v1/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": build_signature_header(gw.WEBHOOK_SECRET, payload)},
        )
    finally:
        set_stripe_gateway(None)

    g = await client.get("/api/v1/billing")
    assert g.json()["budget"]["conversationsThisPeriod"] == 240  # the victim was not reset
