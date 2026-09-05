"""Action-tool connections go dormant on payment loss (Legal §A3 / Architecture
§3.6.7) and are restored when a valid payment method returns.

Three trigger paths, all covered:
  1. dunning exhausting to `reduced` (day-7, driven by a signed invoice.payment_failed
     webhook against a dunning_started_at 8 days in the past).
  2. Stripe `payment_method.detached` webhook (immediate card loss).
  3. DELETE /api/v1/billing/payment-method (admin-initiated removal).

channel_auth / knowledge_source connections are explicitly excluded from the
dormant transition (Legal §A3 only covers action-tool connections) — asserted
against directly. Restore-on-payment (`invoice.paid` / `payment_method.attached`)
uses the PRE-EXISTING `_restore_dormant_connections` path.

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

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


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
    """One Instance + one connected row per action-tool type, PLUS one
    channel_auth and one knowledge_source connection (both connected) so the
    exclusion can be asserted directly."""
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
        for ctype in (*dunning.ACTION_TOOL_CONNECTION_TYPES, "channel_auth", "knowledge_source"):
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


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_dunning_reduced_sends_action_tools_dormant_not_channels(client) -> None:
    admin = await _signup_verify_login(client)
    await _seed_instance_and_connections(admin)

    # Put the account into `retrying` dunning with the clock already past day 7
    # so the signed invoice.payment_failed webhook's advance() lands on `reduced`.
    async with tenant_session(admin) as s:
        acct = await get_or_create_billing_account(s, admin_id=admin)
        acct.dunning_state = dunning.RETRYING
        acct.dunning_started_at = datetime.now(UTC) - timedelta(days=8)
        await s.flush()

    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    try:
        payload = json.dumps(
            {
                "id": "evt_fail_1",
                "type": "invoice.payment_failed",
                "data": {"object": {"metadata": {"admin_id": str(admin)}}},
            }
        ).encode()
        header = build_signature_header(gw.WEBHOOK_SECRET, payload)
        r = await client.post(
            "/api/v1/webhooks/stripe", content=payload, headers={"Stripe-Signature": header}
        )
        assert r.status_code == 200
        assert r.json()["action"] == "payment_failed"
    finally:
        set_stripe_gateway(None)

    statuses = await _connection_statuses(admin)
    for ctype in dunning.ACTION_TOOL_CONNECTION_TYPES:
        assert statuses[ctype] == "dormant", f"{ctype} should be dormant"
    # Explicitly excluded — Legal §A3 only covers action tools.
    assert statuses["channel_auth"] == "connected"
    assert statuses["knowledge_source"] == "connected"

    # Path back: invoice.paid restores dormant → the status each row HAD (F069): these
    # were connected, and a billing dormancy never touched their credentials.
    gw2 = FakeStripeGateway()
    set_stripe_gateway(gw2)
    try:
        payload2 = json.dumps(
            {
                "id": "evt_paid_1",
                "type": "invoice.paid",
                "data": {"object": {"metadata": {"admin_id": str(admin)}}},
            }
        ).encode()
        header2 = build_signature_header(gw2.WEBHOOK_SECRET, payload2)
        r2 = await client.post(
            "/api/v1/webhooks/stripe", content=payload2, headers={"Stripe-Signature": header2}
        )
        assert r2.status_code == 200
        assert r2.json()["action"] == "invoice_paid"
    finally:
        set_stripe_gateway(None)

    restored = await _connection_statuses(admin)
    for ctype in dunning.ACTION_TOOL_CONNECTION_TYPES:
        assert restored[ctype] == "connected"
    assert restored["channel_auth"] == "connected"  # untouched throughout


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_payment_method_detached_webhook_sends_dormant(client) -> None:
    admin = await _signup_verify_login(client)
    await _seed_instance_and_connections(admin)

    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    try:
        payload = json.dumps(
            {
                "id": "evt_detach_1",
                "type": "payment_method.detached",
                "data": {"object": {"metadata": {"admin_id": str(admin)}}},
            }
        ).encode()
        header = build_signature_header(gw.WEBHOOK_SECRET, payload)
        r = await client.post(
            "/api/v1/webhooks/stripe", content=payload, headers={"Stripe-Signature": header}
        )
        assert r.status_code == 200
        assert r.json()["action"] == "payment_method.detached"
    finally:
        set_stripe_gateway(None)

    statuses = await _connection_statuses(admin)
    for ctype in dunning.ACTION_TOOL_CONNECTION_TYPES:
        assert statuses[ctype] == "dormant"
    assert statuses["channel_auth"] == "connected"
    assert statuses["knowledge_source"] == "connected"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_delete_payment_method_sends_dormant_then_pm_attached_restores(client) -> None:
    admin = await _signup_verify_login(client)
    await _seed_instance_and_connections(admin)

    # DELETE /payment-method is idempotent even with no card on file (existing
    # behavior) but must still emit the dormant transition (admin-initiated loss).
    r = await client.delete("/api/v1/billing/payment-method")
    assert r.status_code == 200

    statuses = await _connection_statuses(admin)
    for ctype in dunning.ACTION_TOOL_CONNECTION_TYPES:
        assert statuses[ctype] == "dormant"
    assert statuses["channel_auth"] == "connected"

    # Repayment restore: payment_method.attached restores dormant → prior status (F069).
    gw = FakeStripeGateway()
    set_stripe_gateway(gw)
    try:
        payload = json.dumps(
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
            }
        ).encode()
        header = build_signature_header(gw.WEBHOOK_SECRET, payload)
        r2 = await client.post(
            "/api/v1/webhooks/stripe", content=payload, headers={"Stripe-Signature": header}
        )
        assert r2.status_code == 200
        assert r2.json()["action"] == "payment_method.attached"
    finally:
        set_stripe_gateway(None)

    restored = await _connection_statuses(admin)
    for ctype in dunning.ACTION_TOOL_CONNECTION_TYPES:
        assert restored[ctype] == "connected"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_set_dormant_for_action_tools_is_idempotent(client) -> None:
    """Calling the transition twice only emits the audit event once (no rows
    match the second time — Legal §A3 dormant is a downgrade, not a reset)."""
    admin = await _signup_verify_login(client)
    await _seed_instance_and_connections(admin)

    async with tenant_session(admin) as s:
        first = await dunning.set_dormant_for_action_tools(s, admin_id=admin, reason="test")
    async with tenant_session(admin) as s:
        second = await dunning.set_dormant_for_action_tools(s, admin_id=admin, reason="test")

    assert first == len(dunning.ACTION_TOOL_CONNECTION_TYPES)
    assert second == 0
