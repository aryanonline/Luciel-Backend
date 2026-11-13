"""Dormancy is the day-7 dunning reduction's consequence and nothing else's
(Legal §A3 / Architecture §3.6.7; owner decision D1, 2026-09-14).

D1: removing a payment method changes NOTHING but the 50-conversation cap. So of
the three paths that used to dormant action tools, exactly ONE remains:

  1. dunning exhausting to `reduced` (day-7, driven by a signed invoice.payment_failed
     webhook against a dunning_started_at 8 days in the past) — dormants EXACTLY the
     four action-tool types (calendar, crm, record_source, outbound_webhook).
  2. Stripe `payment_method.detached` webhook — leaves every connection untouched.
  3. DELETE /api/v1/billing/payment-method — leaves every connection untouched.

The BYO channel connections (email_sender = the owner's own mailbox, sms_sender = the
owner's own Twilio, which also carries voice) stay live in EVERY billing state: the
customer pays Microsoft/Twilio directly, so the platform has no standing to pause them.
channel_auth / knowledge_source were never action tools. Restore-on-payment
(`invoice.paid` / `payment_method.attached`) uses the PRE-EXISTING
`_restore_dormant_connections` path and stays green.

FakeStripeGateway only — every webhook is signed with its WEBHOOK_SECRET (verify-
before-route, HARD CONSTRAINT #4). NO real Stripe network call.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.billing import dunning
from app.billing.metering import get_or_create_billing_account
from app.billing.stripe_gateway import FakeStripeGateway, build_signature_header, set_stripe_gateway
from app.db.session import tenant_session
from app.models.connection import InstanceConnection
from app.models.instance import Instance, LucielState
from sqlalchemy import select

# Round 7 WP-1 #5: the webhook receiver ignores events in the mock posture, so every
# signed-webhook test here runs in the "test" posture with the fake injected.
pytestmark = [
    pytest.mark.contract,
    pytest.mark.usefixtures("stripe_test_posture"),
]

# D1: exactly these four go dormant at the reduction; nothing else ever does.
ACTION_TOOLS: frozenset[str] = frozenset({"calendar", "crm", "record_source", "outbound_webhook"})
# The owner's own providers — never touched by billing.
BYO_CHANNELS: tuple[str, ...] = ("email_sender", "sms_sender")
NOT_TOOLS: tuple[str, ...] = ("channel_auth", "knowledge_source")
ALL_TYPES: tuple[str, ...] = (*sorted(ACTION_TOOLS), *BYO_CHANNELS, *NOT_TOOLS)


async def test_d1_action_tool_set_is_exactly_the_four() -> None:
    """The constant IS the decision: email_sender / sms_sender are BYO channels, not
    action tools, and must never re-enter this set."""
    assert dunning.ACTION_TOOL_CONNECTION_TYPES == ACTION_TOOLS


async def _signup_verify_login(client) -> uuid.UUID:
    from app.auth import access

    email = f"dorm-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "t"},
    )
    acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    return acct.id


async def _seed_instance_and_connections(admin: uuid.UUID) -> uuid.UUID:
    """One Instance + one connected row per action-tool type, PLUS the two BYO
    channel connections and one channel_auth / knowledge_source row (all connected)
    so every exclusion can be asserted directly."""
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
        for ctype in ALL_TYPES:
            s.add(
                InstanceConnection(
                    connection_id=uuid.uuid4(),
                    admin_id=admin,
                    instance_id=iid,
                    connection_type=ctype,
                    auth_class="oauth",
                    provider="test_provider",
                    status="connected",
                )
            )
    return iid


async def _connection_statuses(admin: uuid.UUID) -> dict[str, str]:
    async with tenant_session(admin) as s:
        rows = (
            await s.execute(
                select(InstanceConnection.connection_type, InstanceConnection.status).where(
                    InstanceConnection.admin_id == admin
                )
            )
        ).all()
    return {ctype: status for ctype, status in rows}


def _all_connected(statuses: dict[str, str]) -> bool:
    return set(statuses) == set(ALL_TYPES) and all(v == "connected" for v in statuses.values())


async def _post_signed(client, gw: FakeStripeGateway, event: dict) -> dict:
    payload = json.dumps(event).encode()
    header = build_signature_header(gw.WEBHOOK_SECRET, payload)
    r = await client.post(
        "/api/v1/webhooks/stripe", content=payload, headers={"Stripe-Signature": header}
    )
    assert r.status_code == 200
    return r.json()


async def _reduce_via_day7_webhook(client, admin: uuid.UUID) -> None:
    """Put the account into `retrying` with the clock already past day 7 so the signed
    invoice.payment_failed webhook's advance() lands on `reduced`."""
    async with tenant_session(admin) as s:
        acct = await get_or_create_billing_account(s, admin_id=admin)
        acct.dunning_state = dunning.RETRYING
        acct.dunning_started_at = datetime.now(UTC) - timedelta(days=8)
        await s.flush()
    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    try:
        body = await _post_signed(
            client,
            gw,
            {
                "id": f"evt_fail_{uuid.uuid4().hex[:8]}",
                "type": "invoice.payment_failed",
                "data": {"object": {"metadata": {"admin_id": str(admin)}}},
            },
        )
        assert body["action"] == "payment_failed"
    finally:
        set_stripe_gateway(None)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_dunning_reduced_dormants_exactly_the_four_action_tools(client) -> None:
    admin = await _signup_verify_login(client)
    await _seed_instance_and_connections(admin)
    assert _all_connected(await _connection_statuses(admin))

    await _reduce_via_day7_webhook(client, admin)

    statuses = await _connection_statuses(admin)
    dormant = {ctype for ctype, status in statuses.items() if status == "dormant"}
    assert dormant == ACTION_TOOLS, f"dormant set must be exactly the four: {dormant}"
    # D1: the owner's own mailbox and Twilio keep serving through the reduction.
    for ctype in BYO_CHANNELS:
        assert statuses[ctype] == "connected", f"{ctype} is a BYO channel, never dormant"
    for ctype in NOT_TOOLS:
        assert statuses[ctype] == "connected"

    # Path back: invoice.paid restores dormant → the status each row HAD (F069): these
    # were connected, and a billing dormancy never touched their credentials.
    gw2 = FakeStripeGateway()
    set_stripe_gateway(gw2)
    try:
        body = await _post_signed(
            client,
            gw2,
            {
                "id": "evt_paid_1",
                "type": "invoice.paid",
                "data": {"object": {"metadata": {"admin_id": str(admin)}}},
            },
        )
        assert body["action"] == "invoice_paid"
    finally:
        set_stripe_gateway(None)
    assert _all_connected(await _connection_statuses(admin))


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_payment_method_detached_webhook_leaves_every_connection_untouched(client) -> None:
    """D1: a Stripe-side detach reverts billing_state and clears the display tuple —
    and changes nothing else."""
    admin = await _signup_verify_login(client)
    await _seed_instance_and_connections(admin)
    async with tenant_session(admin) as s:
        acct = await get_or_create_billing_account(s, admin_id=admin)
        acct.billing_state = "payg_enabled"
        acct.pm_brand = "visa"
        acct.pm_last4 = "4242"
        await s.flush()

    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    try:
        body = await _post_signed(
            client,
            gw,
            {
                "id": "evt_detach_1",
                "type": "payment_method.detached",
                "data": {"object": {"metadata": {"admin_id": str(admin)}}},
            },
        )
        assert body["action"] == "payment_method.detached"
    finally:
        set_stripe_gateway(None)

    async with tenant_session(admin) as s:
        acct = await get_or_create_billing_account(s, admin_id=admin)
        assert acct.billing_state == "free_cap"
        assert acct.pm_brand is None
    assert _all_connected(await _connection_statuses(admin))


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_delete_payment_method_leaves_every_connection_untouched(client) -> None:
    """D1: the admin's own card removal changes nothing but the cap. The read that
    comes back says free_cap; every connection still says connected."""
    admin = await _signup_verify_login(client)
    await _seed_instance_and_connections(admin)
    async with tenant_session(admin) as s:
        acct = await get_or_create_billing_account(s, admin_id=admin)
        acct.billing_state = "payg_enabled"
        acct.pm_brand = "visa"
        acct.pm_last4 = "4242"
        await s.flush()

    set_stripe_gateway(FakeStripeGateway())
    try:
        r = await client.delete("/api/v1/billing/payment-method")
        assert r.status_code == 200
        assert r.json()["budget"]["billingState"] == "free_cap"
    finally:
        set_stripe_gateway(None)

    assert _all_connected(await _connection_statuses(admin))


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_card_returning_restores_the_dunning_dormancy(client) -> None:
    """The restore path is unchanged: connections the day-7 reduction sent dormant come
    back to their prior status on payment_method.attached (Legal §A3 / F069)."""
    admin = await _signup_verify_login(client)
    await _seed_instance_and_connections(admin)
    await _reduce_via_day7_webhook(client, admin)
    statuses = await _connection_statuses(admin)
    assert {c for c, st in statuses.items() if st == "dormant"} == ACTION_TOOLS

    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    try:
        body = await _post_signed(
            client,
            gw,
            {
                "id": "evt_attach_1",
                "type": "payment_method.attached",
                "data": {
                    "object": {
                        "metadata": {"admin_id": str(admin)},
                        "card": {
                            "brand": "visa",
                            "last4": "4242",
                            "exp_month": 1,
                            "exp_year": 2030,
                        },
                    }
                },
            },
        )
        assert body["action"] == "payment_method.attached"
    finally:
        set_stripe_gateway(None)

    assert _all_connected(await _connection_statuses(admin))
    async with tenant_session(admin) as s:
        acct = await get_or_create_billing_account(s, admin_id=admin)
        assert acct.dunning_state == dunning.NONE  # a new card is the remedy (F012)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_set_dormant_for_action_tools_is_idempotent(client) -> None:
    """Calling the transition twice only emits the audit event once (no rows
    match the second time — Legal §A3 dormant is a downgrade, not a reset). The
    count is the four action tools — the BYO channels are not in the set."""
    admin = await _signup_verify_login(client)
    await _seed_instance_and_connections(admin)

    async with tenant_session(admin) as s:
        first = await dunning.set_dormant_for_action_tools(s, admin_id=admin, reason="test")
    async with tenant_session(admin) as s:
        second = await dunning.set_dormant_for_action_tools(s, admin_id=admin, reason="test")

    assert first == len(ACTION_TOOLS) == 4
    assert second == 0
    statuses = await _connection_statuses(admin)
    for ctype in BYO_CHANNELS:
        assert statuses[ctype] == "connected"
