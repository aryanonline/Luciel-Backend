"""Deterministic FakeStripeGateway behavior (HARD CONSTRAINT #1, #3, #4).

NO real Stripe network call. Webhook signature verify-before-route, idempotent
metered usage reporting, and idempotent payment-method removal.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from hashlib import sha256

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
    """Since API version 2025-03-31 "basil" (so also on the later version the gateway
    pins), metered prices must be meter-backed and the usage-records endpoint is gone.
    The value must go to /v1/billing/meter_events, keyed on the customer, with the
    identifier carried through so Stripe can dedupe a retry."""
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
        json.dumps({"STRIPE_TEST_SECRET_KEY": "sk_test_fixture"}),  # no webhook secret
    )
    get_settings.cache_clear()
    set_stripe_gateway(None)
    try:
        with pytest.raises(RuntimeError, match="STRIPE_TEST_WEBHOOK_SECRET") as refused:
            get_stripe_gateway()
        # The publishable key is optional (round 7 WP-7): never named as missing.
        assert "STRIPE_TEST_PUBLISHABLE_KEY" not in str(refused.value)
    finally:
        set_stripe_gateway(None)
        get_settings.cache_clear()


def test_test_posture_binds_without_a_publishable_key(monkeypatch) -> None:
    """Round 7 WP-7: STRIPE_TEST_PUBLISHABLE_KEY has no reader on the server path
    (Checkout and the Billing Portal are server-created redirects), yet the binder
    refused the whole "test" posture without it. The two keys the server DOES use
    are enough."""
    from app.config import get_settings

    monkeypatch.setenv("LUCIEL_STRIPE_INTEGRATION_POSTURE", "test")
    monkeypatch.setenv(
        "LUCIEL_PLATFORM_SECRET",
        json.dumps(
            {
                "STRIPE_TEST_SECRET_KEY": "sk_test_fixture",
                "STRIPE_TEST_WEBHOOK_SECRET": "whsec_fixture",
            }
        ),
    )
    get_settings.cache_clear()
    set_stripe_gateway(None)
    try:
        assert isinstance(get_stripe_gateway(), RealStripeGateway)
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


def test_signature_diagnostic_metadata_is_ignored_not_trusted() -> None:
    """stripe-python 17 reads optional diagnostic fields Stripe may append to the
    Stripe-Signature header: ``secret=`` (masked), ``destination=`` and ``payload=`` (a
    body-hash suffix). The verifier is our own, so it must keep deciding on ``t`` and
    ``v1`` alone: no crash on the extra fields, and no field standing in for a signature
    or rescuing a stale one."""
    gw = FakeStripeGateway()
    payload = json.dumps({"id": "evt_m", "type": "invoice.paid", "data": {"object": {}}}).encode()
    meta = f",secret=whsec_****abcd,destination=we_test, payload={sha256(payload).hexdigest()[-8:]}"

    signed = build_signature_header(gw.WEBHOOK_SECRET, payload) + meta
    assert gw.verify_and_parse(payload=payload, signature_header=signed).event_id == "evt_m"

    with pytest.raises(SignatureError):  # wrong secret, metadata attached
        gw.verify_and_parse(
            payload=payload, signature_header=build_signature_header("whsec_wrong", payload) + meta
        )
    with pytest.raises(SignatureError):  # stale, metadata attached
        stale = int(time.time()) - SIGNATURE_TOLERANCE_SECONDS - 60
        gw.verify_and_parse(
            payload=payload,
            signature_header=build_signature_header(gw.WEBHOOK_SECRET, payload, stale) + meta,
        )
    with pytest.raises(SignatureError):  # metadata alone is not a signature
        gw.verify_and_parse(payload=payload, signature_header=meta.lstrip(","))


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


# --- round 7 WP-1 #5: the card-update portal, the per-attempt subscription key -- #


class _PortalStripeClient:
    """Stands in for the stripe module's billing_portal namespace. NO network."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        outer = self

        class _Session:
            @staticmethod
            def create(**kwargs):
                outer.calls.append(kwargs)
                return {
                    "id": "bps_test_1",
                    "url": "https://billing.stripe.com/p/session/bps_test_1",
                }

        class _BillingPortal:
            Session = _Session

        self.billing_portal = _BillingPortal()


def test_real_gateway_opens_the_portal_against_the_bound_configuration(monkeypatch) -> None:
    """Stripe TEST mode has no default portal configuration, so the test posture must
    name the operator-provisioned one; an EMPTY id is omitted rather than sent, so a
    live account with a default is not handed an empty string Stripe would reject."""
    from app.config import get_settings

    client = _PortalStripeClient()
    gw = _real_gateway_with(client)  # type: ignore[arg-type]

    monkeypatch.setattr(get_settings(), "stripe_portal_configuration_id", "bpc_test_cfg")
    session = asyncio.run(
        gw.create_portal_session(customer_id="cus_x", return_url="https://app/dashboard/billing")
    )
    assert session.url == "https://billing.stripe.com/p/session/bps_test_1"
    assert session.session_id == "bps_test_1"
    assert client.calls == [
        {
            "customer": "cus_x",
            "return_url": "https://app/dashboard/billing",
            "configuration": "bpc_test_cfg",
        }
    ]

    monkeypatch.setattr(get_settings(), "stripe_portal_configuration_id", "")
    asyncio.run(gw.create_portal_session(customer_id="cus_x", return_url="https://app/b"))
    assert client.calls[-1] == {"customer": "cus_x", "return_url": "https://app/b"}


def test_portal_configuration_id_comes_from_the_platform_secret(monkeypatch) -> None:
    """Same publication path as the price: the operator binds it by rotating the secret."""
    from app.billing.stripe_gateway import portal_configuration_id
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        assert portal_configuration_id() == ""
    finally:
        get_settings.cache_clear()
    monkeypatch.setenv(
        "LUCIEL_PLATFORM_SECRET", json.dumps({"STRIPE_PORTAL_CONFIGURATION_ID": "bpc_from_secret"})
    )
    get_settings.cache_clear()
    try:
        assert portal_configuration_id() == "bpc_from_secret"
    finally:
        get_settings.cache_clear()


def test_fake_portal_session_is_deterministic_and_changes_no_card_state() -> None:
    gw = FakeStripeGateway()

    async def run() -> tuple[str, str]:
        cid = await gw.create_customer(admin_id=uuid.UUID(int=7), email="x@y.com")
        first = await gw.create_portal_session(customer_id=cid, return_url="https://r")
        second = await gw.create_portal_session(customer_id=cid, return_url="https://r")
        assert first == second
        assert await gw.get_payment_method(customer_id=cid) is None  # nothing attached
        return cid, first.url

    cid, url = asyncio.run(run())
    assert url == f"https://billing.stripe.com/p/session/bps_fake_{cid.removeprefix('cus_fake_')}"
    assert gw.portal_sessions == [(cid, "https://r"), (cid, "https://r")]


class _SubscriptionStripeClient:
    """Records Subscription.create calls; the active-subscription lookup finds none."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        outer = self

        class _Subscription:
            @staticmethod
            def list(**kwargs):
                return {"data": []}

            @staticmethod
            def create(**kwargs):
                outer.created.append(kwargs)
                return {"id": f"sub_{len(outer.created)}"}

        self.Subscription = _Subscription


def test_subscription_create_idempotency_key_is_unique_per_attempt(monkeypatch) -> None:
    """The key used to be the bare customer id. Stripe replays a keyed request's
    ORIGINAL response for 24 hours, so a card removed (subscription cancelled with its
    final invoice) and re-added the same day had its next block "created" as the
    cancelled subscription — which invoices nothing. Every attempt now carries a fresh
    key; the active-subscription lookup is what keeps a retry from minting a second one."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "stripe_payg_price_id", "price_bound")
    client = _SubscriptionStripeClient()
    gw = _real_gateway_with(client)  # type: ignore[arg-type]

    first = asyncio.run(gw.ensure_metered_subscription(customer_id="cus_k"))
    second = asyncio.run(gw.ensure_metered_subscription(customer_id="cus_k"))
    assert (first, second) == ("sub_1", "sub_2")
    keys = [c["idempotency_key"] for c in client.created]
    assert all(k.startswith("payg-sub:cus_k:") for k in keys)
    assert len(set(keys)) == 2, "a cancel-then-recreate must never reuse a spent key"
    assert all(c["items"] == [{"price": "price_bound"}] for c in client.created)


class _NoDefaultCardClient:
    """A customer with NO invoice default (a Checkout setup-mode card before the
    attached webhook made it the default, F006) and the given attached cards."""

    def __init__(self, cards: list[dict]) -> None:
        class Customer:
            @staticmethod
            def retrieve(cid):
                return {"invoice_settings": {"default_payment_method": None}}

        class PaymentMethod:
            @staticmethod
            def list(**kwargs):
                assert kwargs == {"customer": "cus_x", "type": "card", "limit": 1}
                return {"data": cards}

            @staticmethod
            def retrieve(pmid):
                raise AssertionError("no default to retrieve")

        self.Customer = Customer
        self.PaymentMethod = PaymentMethod


def test_get_payment_method_falls_back_to_the_newest_attached_card_without_a_default() -> None:
    """Between the Checkout redirect and the attached webhook the card IS on file at
    Stripe but is not yet the invoice default. The read must see it (the billing-read
    reconcile depends on this), the same way remove_payment_method already treats a
    card attached with no default as a card to detach."""
    gw = _real_gateway_with(  # type: ignore[arg-type]
        _NoDefaultCardClient(
            [
                {
                    "id": "pm_new",
                    "card": {"brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2030},
                }
            ]
        )
    )
    disp = asyncio.run(gw.get_payment_method(customer_id="cus_x"))
    assert disp is not None and (disp.brand, disp.last4, disp.exp_month, disp.exp_year) == (
        "visa",
        "4242",
        12,
        2030,
    )

    gw = _real_gateway_with(_NoDefaultCardClient([]))  # type: ignore[arg-type]
    assert asyncio.run(gw.get_payment_method(customer_id="cus_x")) is None
