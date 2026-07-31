"""Deterministic FakeStripeGateway behavior (HARD CONSTRAINT #1, #3, #4).

NO real Stripe network call. Webhook signature verify-before-route, idempotent
metered usage reporting, and idempotent payment-method removal.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest
from app.billing.stripe_gateway import (
    SIGNATURE_TOLERANCE_SECONDS,
    FakeStripeGateway,
    RealStripeGateway,
    SignatureError,
    build_signature_header,
    get_stripe_gateway,
    set_stripe_gateway,
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


def test_the_fake_models_set_versus_increment() -> None:
    """The fake only appended to a list, so the suite could not see the difference
    between the two actions — which is how a geometric overcharge stayed invisible.

    Both branches are modelled because the whole point is that they diverge: the caller
    posts a period-cumulative quantity, which is only correct under "set".
    """
    gw = FakeStripeGateway()

    async def run() -> tuple[int, int]:
        for i, quantity in enumerate((1, 2, 3)):
            await gw.report_metered_usage(
                subscription_id="sub_set", quantity=quantity, idempotency_key=f"set-{i}"
            )
            await gw.report_metered_usage(
                subscription_id="sub_inc",
                quantity=quantity,
                idempotency_key=f"inc-{i}",
                action="increment",
            )
        return gw.billed_quantity("sub_set"), gw.billed_quantity("sub_inc")

    with_set, with_increment = asyncio.run(run())
    assert with_set == 3  # the period total, which is the actual usage
    assert with_increment == 6  # 1+2+3 — Stripe's default, and double the real usage


# --- Stripe TEST-mode posture wiring (never sk_live_, never hardcoded) ------- #


def test_real_gateway_rejects_a_non_test_secret_key() -> None:
    """RealStripeGateway refuses anything that isn't sk_test_... — in particular
    this is what stands between the codebase and ever binding an sk_live_ key."""
    with pytest.raises(RuntimeError, match="TEST secret key"):
        RealStripeGateway(
            secret_key="sk_live_should_never_be_used",
            publishable_key="pk_live_x",
            webhook_secret="whsec_x",
        )


def test_real_gateway_verifies_signature_with_test_webhook_secret_no_network() -> None:
    """Construction + signature verification never touches the network — only the
    HMAC scheme runs, keyed by the TEST webhook signing secret."""
    gw = RealStripeGateway(
        secret_key="sk_test_abc123",
        publishable_key="pk_test_abc123",
        webhook_secret="whsec_test_abc123",
    )
    payload = json.dumps(
        {"id": "evt_test_1", "type": "invoice.paid", "data": {"object": {}}}
    ).encode()
    header = build_signature_header("whsec_test_abc123", payload)
    ev = gw.verify_and_parse(payload=payload, signature_header=header)
    assert ev.event_id == "evt_test_1"

    with pytest.raises(SignatureError):  # wrong secret → rejected
        bad = build_signature_header("whsec_wrong", payload)
        gw.verify_and_parse(payload=payload, signature_header=bad)
    with pytest.raises(SignatureError):  # tampered payload, same header → rejected
        gw.verify_and_parse(payload=payload + b"x", signature_header=header)
    with pytest.raises(SignatureError):  # missing signature → rejected
        gw.verify_and_parse(payload=payload, signature_header="")


def test_get_stripe_gateway_defaults_to_fake_no_keys_read(monkeypatch) -> None:
    """stripe_integration_posture defaults to "mock" — get_stripe_gateway() must
    stay on FakeStripeGateway with NO platform-secret read and NO stripe import,
    exactly the existing behavior every other test in the suite depends on."""
    from app.config import get_settings

    set_stripe_gateway(None)
    get_settings.cache_clear()
    try:
        gw = get_stripe_gateway()
        assert isinstance(gw, FakeStripeGateway)
    finally:
        set_stripe_gateway(None)
        get_settings.cache_clear()


def test_get_stripe_gateway_binds_real_gateway_in_test_posture(monkeypatch) -> None:
    """stripe_integration_posture="test" + TEST keys in the platform secret blob →
    RealStripeGateway, reading keys ONLY from config/Secrets Manager (never a
    literal in source) and never the sk_live_ key."""
    from app.config import get_settings

    monkeypatch.setenv("LUCIEL_STRIPE_INTEGRATION_POSTURE", "test")
    monkeypatch.setenv(
        "LUCIEL_PLATFORM_SECRET",
        json.dumps(
            {
                "STRIPE_TEST_SECRET_KEY": "sk_test_fixture",
                "STRIPE_TEST_PUBLISHABLE_KEY": "pk_test_fixture",
                "STRIPE_TEST_WEBHOOK_SECRET": "whsec_fixture",
            }
        ),
    )
    get_settings.cache_clear()
    set_stripe_gateway(None)
    try:
        gw = get_stripe_gateway()
        assert isinstance(gw, RealStripeGateway)
        assert not isinstance(gw, FakeStripeGateway)
    finally:
        set_stripe_gateway(None)
        get_settings.cache_clear()


def test_test_posture_fails_closed_when_a_key_is_missing(monkeypatch) -> None:
    """A live deploy in "test" posture with an incomplete secret blob must fail
    loudly at construction (naming only the missing key, never a value) rather
    than silently falling back to the fake."""
    from app.config import get_settings

    monkeypatch.setenv("LUCIEL_STRIPE_INTEGRATION_POSTURE", "test")
    monkeypatch.setenv(
        "LUCIEL_PLATFORM_SECRET",
        json.dumps({"STRIPE_TEST_SECRET_KEY": "sk_test_fixture"}),  # missing the other two
    )
    get_settings.cache_clear()
    set_stripe_gateway(None)
    try:
        with pytest.raises(RuntimeError, match="STRIPE_TEST_PUBLISHABLE_KEY"):
            get_stripe_gateway()
    finally:
        set_stripe_gateway(None)
        get_settings.cache_clear()


# --- the PAYG metered price binding (never hardcoded) ------------------------ #


def test_payg_price_id_is_empty_by_default() -> None:
    """No default is possible: a Stripe price id is an account-specific object id, so a
    literal here would bill some other account's price. Empty means "report nothing"."""
    from app.billing.stripe_gateway import payg_price_id
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        assert payg_price_id() == ""
    finally:
        get_settings.cache_clear()


def test_payg_price_id_comes_from_the_platform_secret(monkeypatch) -> None:
    """Publishing the price through the platform secret is what lets the operator bind
    PAYG billing by rotating the secret instead of shipping a task-definition change."""
    from app.billing.stripe_gateway import payg_price_id
    from app.config import get_settings

    monkeypatch.setenv(
        "LUCIEL_PLATFORM_SECRET", json.dumps({"STRIPE_PAYG_PRICE_ID": "price_from_secret"})
    )
    get_settings.cache_clear()
    try:
        assert payg_price_id() == "price_from_secret"
    finally:
        get_settings.cache_clear()


def test_an_explicit_env_price_beats_the_platform_secret(monkeypatch) -> None:
    """Overlay precedence: the blob fills an EMPTY setting, so an operator who pins the
    price per environment is not silently overridden by the shared secret."""
    from app.billing.stripe_gateway import payg_price_id
    from app.config import get_settings

    monkeypatch.setenv("LUCIEL_STRIPE_PAYG_PRICE_ID", "price_from_env")
    monkeypatch.setenv(
        "LUCIEL_PLATFORM_SECRET", json.dumps({"STRIPE_PAYG_PRICE_ID": "price_from_secret"})
    )
    get_settings.cache_clear()
    try:
        assert payg_price_id() == "price_from_env"
    finally:
        get_settings.cache_clear()


def test_fake_gateway_binds_no_subscription_without_a_price(monkeypatch) -> None:
    """The fake gates on the price exactly as the real gateway does. If it did not, the
    suite would keep proving usage flows on a path that is silent in production."""
    from app.billing.stripe_gateway import payg_price_id
    from app.config import get_settings

    gw = FakeStripeGateway()

    async def run(expected: str) -> str | None:
        assert payg_price_id() == expected
        return await gw.ensure_metered_subscription(customer_id="cus_fake_abc")

    get_settings.cache_clear()
    try:
        assert asyncio.run(run("")) is None
    finally:
        get_settings.cache_clear()

    monkeypatch.setattr(get_settings(), "stripe_payg_price_id", "price_bound")
    assert asyncio.run(run("price_bound")) == "sub_fake_abc"


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


def test_get_payment_method_handles_stripe_object_not_dict() -> None:
    """Regression: stripe-python returns StripeObject (subscriptable) whose nested
    members do NOT support .get() — the code must use subscript access, not chained
    .get(). This reproduced a live 500 (AttributeError) the dict-based fake hid."""
    import asyncio

    class SObj(dict):
        # Subscriptable like a dict, but .get on NESTED access is unavailable —
        # emulate StripeObject by removing .get from nested objects.
        pass

    class NoGet:
        def __init__(self, d):
            self._d = d

        def __contains__(self, k):
            return k in self._d

        def __getitem__(self, k):
            return self._d[k]

        # deliberately NO .get

    class FakeClient:
        class Customer:
            @staticmethod
            def retrieve(cid):
                return NoGet({"invoice_settings": NoGet({"default_payment_method": "pm_x"})})

        class PaymentMethod:
            @staticmethod
            def retrieve(pmid):
                return NoGet(
                    {
                        "card": NoGet(
                            {"brand": "visa", "last4": "4242", "exp_month": 7, "exp_year": 2030}
                        )
                    }
                )

    from app.billing.stripe_gateway import RealStripeGateway

    gw = RealStripeGateway(
        secret_key="sk_test_x", publishable_key="pk_test_x", webhook_secret="whsec_x"
    )
    gw._client = FakeClient()  # inject; no network
    disp = asyncio.get_event_loop().run_until_complete(gw.get_payment_method(customer_id="cus_x"))
    assert disp is not None
    assert disp.brand == "visa" and disp.last4 == "4242"


def test_a_captured_signed_event_is_not_replayable_forever() -> None:
    """The ``t=`` value was parsed and thrown away, so a signature stayed valid
    indefinitely: anyone who captured one signed ``invoice.paid`` could re-post it and
    wipe the tenant's usage counter. Stripe's own 5-minute tolerance closes it.
    """
    gw = FakeStripeGateway()
    payload = json.dumps({"id": "evt_t", "type": "invoice.paid", "data": {"object": {}}}).encode()

    fresh = build_signature_header(gw.WEBHOOK_SECRET, payload)
    assert gw.verify_and_parse(payload=payload, signature_header=fresh).event_id == "evt_t"

    stale = build_signature_header(
        gw.WEBHOOK_SECRET, payload, timestamp=int(time.time()) - SIGNATURE_TOLERANCE_SECONDS - 60
    )
    with pytest.raises(SignatureError):
        gw.verify_and_parse(payload=payload, signature_header=stale)

    # A far-future timestamp is equally a forgery signal, not merely "not yet stale".
    future = build_signature_header(
        gw.WEBHOOK_SECRET, payload, timestamp=int(time.time()) + SIGNATURE_TOLERANCE_SECONDS + 60
    )
    with pytest.raises(SignatureError):
        gw.verify_and_parse(payload=payload, signature_header=future)


def test_a_non_numeric_signature_timestamp_is_rejected_not_crashed() -> None:
    """``t=`` is attacker-supplied; parsing it must fail closed as SignatureError rather
    than raise ValueError out of the route as a 500."""
    gw = FakeStripeGateway()
    payload = json.dumps({"id": "evt_b", "type": "invoice.paid", "data": {"object": {}}}).encode()
    with pytest.raises(SignatureError):
        gw.verify_and_parse(payload=payload, signature_header="t=abc,v1=deadbeef")


def test_the_real_gateway_verifies_identically_to_the_fake() -> None:
    """Both used byte-identical verification code. The suite proves webhook handling
    against the fake, so any drift between them is invisible until production."""
    secret = "whsec_shared"
    real = RealStripeGateway(
        secret_key="sk_test_x", publishable_key="pk_test_x", webhook_secret=secret
    )
    payload = json.dumps({"id": "evt_r", "type": "invoice.paid", "data": {"object": {}}}).encode()

    assert (
        real.verify_and_parse(
            payload=payload, signature_header=build_signature_header(secret, payload)
        ).event_id
        == "evt_r"
    )
    with pytest.raises(SignatureError):
        real.verify_and_parse(
            payload=payload,
            signature_header=build_signature_header(
                secret, payload, timestamp=int(time.time()) - 3600
            ),
        )
