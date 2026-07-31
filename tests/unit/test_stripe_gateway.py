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
    METER_EVENT_NAME,
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


def test_meter_event_reporting_is_idempotent_on_the_identifier() -> None:
    """Additivity makes the identifier load-bearing: a retry that is not deduped does
    not merely re-record the event, it ADDS its value to the meter a second time."""
    gw = FakeStripeGateway()

    async def run() -> tuple[bool, bool]:
        first = await gw.report_meter_event(customer_id="cus_1", value=2, identifier="id1")
        second = await gw.report_meter_event(customer_id="cus_1", value=2, identifier="id1")
        return first, second

    first, second = asyncio.run(run())
    assert first is True  # first event applied
    assert second is False  # same identifier → dedup no-op (no double-bill)
    assert gw.billed_quantity("cus_1") == 2  # NOT 4
    assert [e.value for e in gw.meter_events] == [2]


def test_the_fake_models_additive_meter_semantics() -> None:
    """The meter SUMS every event in the period, so the fake must too.

    A fake that stored the last value instead would let a caller post period totals and
    still look correct in the suite, which is exactly the drift that hid the previous
    ``action`` bug — and posting totals to an additive meter is a geometric overcharge.
    """
    gw = FakeStripeGateway()

    async def run() -> int:
        for i, delta in enumerate((1, 1, 1)):
            await gw.report_meter_event(customer_id="cus_sum", value=delta, identifier=f"d-{i}")
        return gw.billed_quantity("cus_sum")

    assert asyncio.run(run()) == 3  # three one-block deltas == three blocks
    assert all(e.event_name == METER_EVENT_NAME for e in gw.meter_events)


# --- the real gateway posts Billing Meter events, not usage records ---------- #


class _RecordingStripeClient:
    """Stands in for the stripe module. Records meter events; NO network.

    Deliberately exposes no ``SubscriptionItem`` at all, so any code that reaches for
    the removed usage-records endpoint fails here with AttributeError instead of
    silently "passing" against a permissive mock.
    """

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self._error = error
        outer = self

        class _MeterEvent:
            @staticmethod
            def create(**kwargs):
                outer.calls.append(kwargs)
                if outer._error is not None:
                    raise outer._error
                return {"identifier": kwargs.get("identifier")}

        class _Billing:
            MeterEvent = _MeterEvent

        self.billing = _Billing()


def _real_gateway_with(client: _RecordingStripeClient) -> RealStripeGateway:
    gw = RealStripeGateway(
        secret_key="sk_test_x", publishable_key="pk_test_x", webhook_secret="whsec_x"
    )
    gw._client = client  # inject; no network
    return gw


def test_real_gateway_posts_a_meter_event_with_the_configured_event_name() -> None:
    """The account is pinned to API version 2025-03-31 "basil", where metered prices
    must be meter-backed and the usage-records endpoint is gone. The value must go to
    /v1/billing/meter_events, keyed on the customer, with the identifier carried through
    so Stripe can dedupe a retry."""
    client = _RecordingStripeClient()
    gw = _real_gateway_with(client)

    assert asyncio.run(gw.report_meter_event(customer_id="cus_x", value=2, identifier="u:1-3"))

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["event_name"] == METER_EVENT_NAME
    assert call["identifier"] == "u:1-3"
    # Stripe requires payload values to be strings, including the numeric one.
    assert call["payload"] == {"stripe_customer_id": "cus_x", "value": "2"}


def test_a_duplicate_identifier_is_reported_as_a_dedup_not_a_failure() -> None:
    """A retry after a timeout re-posts a spent identifier. Stripe rejects it, and that
    rejection means Stripe ALREADY HAS the value — so it must surface as a dedup (False)
    and let the caller advance its watermark. Raising instead would retry the same spent
    identifier forever and leave every later block unbilled behind it."""
    client = _RecordingStripeClient(error=ValueError("An event with identifier X already exists."))
    gw = _real_gateway_with(client)

    assert asyncio.run(gw.report_meter_event(customer_id="cus_x", value=1, identifier="dup")) is (
        False
    )


def test_a_genuine_stripe_failure_still_propagates() -> None:
    """The duplicate check must stay narrow. Swallowing an ordinary failure would
    advance the watermark past usage Stripe never received — silently un-billing it."""
    client = _RecordingStripeClient(error=RuntimeError("service unavailable"))
    gw = _real_gateway_with(client)

    with pytest.raises(RuntimeError, match="service unavailable"):
        asyncio.run(gw.report_meter_event(customer_id="cus_x", value=1, identifier="boom"))


def test_nothing_in_the_codebase_calls_the_removed_usage_records_endpoint() -> None:
    """``SubscriptionItem.create_usage_record`` was REMOVED in API version 2025-03-31.

    A call to it cannot fail in the suite (the fake never modelled the real endpoint),
    so only a source-level check can keep it from creeping back in — the previous
    implementation looked perfectly healthy in CI while being incapable of billing
    anything at all. Matching call syntax rather than the bare name leaves the history
    describable in prose.
    """
    from pathlib import Path

    app_dir = Path(__file__).resolve().parents[2] / "app"
    offenders = [
        str(p.relative_to(app_dir))
        for p in app_dir.rglob("*.py")
        if "create_usage_record(" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


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
