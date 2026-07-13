"""Deterministic FakeStripeGateway behavior (HARD CONSTRAINT #1, #3, #4).

NO real Stripe network call. Webhook signature verify-before-route, idempotent
metered usage reporting, and idempotent payment-method removal.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from app.billing.stripe_gateway import (
    FakeStripeGateway,
    SignatureError,
    build_signature_header,
)


def test_signature_verification_rejects_tampered_or_missing() -> None:
    gw = FakeStripeGateway()
    payload = json.dumps({"id": "evt_x", "type": "invoice.paid", "data": {"object": {}}}).encode()
    header = build_signature_header(gw.WEBHOOK_SECRET, payload)

    ev = gw.verify_and_parse(payload=payload, signature_header=header)
    assert ev.event_id == "evt_x"
    assert ev.type == "invoice.paid"

    with pytest.raises(SignatureError):  # tampered payload, same header
        gw.verify_and_parse(payload=payload + b"x", signature_header=header)
    with pytest.raises(SignatureError):  # missing header
        gw.verify_and_parse(payload=payload, signature_header="")
    with pytest.raises(SignatureError):  # wrong secret
        bad = build_signature_header("whsec_wrong", payload)
        gw.verify_and_parse(payload=payload, signature_header=bad)


def test_metered_usage_reporting_is_idempotent() -> None:
    gw = FakeStripeGateway()

    async def run() -> tuple[bool, bool]:
        first = await gw.report_metered_usage(
            subscription_id="sub_1", quantity=2, idempotency_key="k1"
        )
        second = await gw.report_metered_usage(
            subscription_id="sub_1", quantity=2, idempotency_key="k1"
        )
        return first, second

    first, second = asyncio.run(run())
    assert first is True  # first report applied
    assert second is False  # same key → dedup no-op (no double-bill)


def test_remove_payment_method_idempotent() -> None:
    gw = FakeStripeGateway()

    async def run() -> tuple[bool, bool]:
        admin = uuid.uuid4()
        cid = await gw.create_customer(admin_id=admin, email="x@y.com")
        await gw.create_checkout_session(
            customer_id=cid, success_url="https://s", cancel_url="https://c"
        )
        first = await gw.remove_payment_method(customer_id=cid)
        second = await gw.remove_payment_method(customer_id=cid)
        return first, second

    first, second = asyncio.run(run())
    assert first is True  # had a card (checkout attached one)
    assert second is False  # already removed → idempotent
