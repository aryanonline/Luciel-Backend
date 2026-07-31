"""Stripe gateway seam (Architecture §3.4.1b — billing/metering).

ALL Stripe interaction goes through the :class:`StripeGateway` Protocol. This is a
swappable seam (HARD CONSTRAINT, Arc 18 objective #1):

  * CI / every test uses :class:`FakeStripeGateway` — a DETERMINISTIC, in-process
    fake. There is NO real Stripe network call anywhere in the test path.
  * A real stripe-python TEST-MODE impl (:class:`RealStripeGateway`, below) binds
    behind the same interface when ``config.stripe_integration_posture == "test"``.
    It reads the Stripe TEST secret/publishable/webhook-signing keys from the
    platform secret blob (mirrors ``app.runtime.provider_factory``'s LLM GATE 2
    cutover shape) — NEVER a hardcoded literal, and NEVER the sk_live_ key (that
    binding does not exist in this codebase). It is NOT invoked by the test suite
    (tests always inject FakeStripeGateway); CI leaves ``stripe_integration_posture``
    at its "mock" default so no key is read and no network call is possible.
  * NO live/production Stripe — ever (objective DO-NOT); "live" is not a value
    ``stripe_integration_posture`` accepts.

Card-data hygiene (HARD CONSTRAINT #2): the gateway returns ONLY a display tuple
(brand + last4 + expMonth + expYear) plus Stripe ids (customer / payment-method).
No PAN, no CVV, no full card number is ever returned or stored — Stripe holds the
card; we hold pointers + a display tuple (Vision §7 / objective).

Webhook verification (HARD CONSTRAINT #4): :meth:`StripeGateway.verify_and_parse`
verifies the ``Stripe-Signature`` (HMAC-SHA256 over ``timestamp.payload`` with the
endpoint signing secret, constant-time compared — the Stripe scheme, mirroring the
Arc 13 verify-before-route discipline) BEFORE any event is acted on. Unverified →
raises, the route rejects.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.config import FAKE_STRIPE_WEBHOOK_SECRET


@dataclass(frozen=True)
class PaymentMethodDisplay:
    """Display-only card tuple (§3.4.1b card hygiene). NO PAN / CVV — ever."""

    brand: str
    last4: str
    exp_month: int
    exp_year: int


@dataclass(frozen=True)
class CheckoutSession:
    """Hosted Stripe Checkout result — the URL the UI redirects to (CJ §6)."""

    url: str
    session_id: str


METER_EVENT_NAME = "luciel_payg_blocks"
"""``event_name`` of the Stripe Billing Meter every PAYG usage event is posted to.

Ops creates the meter with exactly this ``event_name`` and binds the PAYG price to it;
the name is the contract between the two, so it lives here rather than in config — a
per-environment override would silently meter into a meter no price reads.
"""


@dataclass(frozen=True)
class MeterEvent:
    """One accepted Billing Meter event (§3.4.1b).

    ``value`` is a DELTA of 100-conversation blocks, never a period total and never a
    dollar amount: meter events are additive, so Stripe sums every event in the period
    and multiplies by the meter-backed price.
    """

    event_name: str
    customer_id: str
    value: int
    # Stripe dedupes on this within the meter's window, so it is the thing that makes a
    # retry after a timeout safe. See ``metering.meter_event_identifier``.
    identifier: str


def payg_price_id() -> str:
    """The configured Stripe metered price for PAYG overage, or "" when unbound.

    Empty is the honest default: the price is an account-specific Stripe object id, so
    there is nothing sane to fall back to. Callers must treat "" as "report no usage"
    (§3.4.1b) — see :meth:`StripeGateway.ensure_metered_subscription`.
    """
    from app.config import get_settings  # noqa: PLC0415 — avoids an import cycle

    return get_settings().stripe_payg_price_id or ""


@dataclass(frozen=True)
class StripeEvent:
    """A verified, parsed Stripe webhook event (signature already checked)."""

    event_id: str
    type: str
    data: dict[str, object]


class SignatureError(Exception):
    """Raised when a webhook signature fails verification — route must reject."""


@runtime_checkable
class StripeGateway(Protocol):
    """The one seam every Stripe interaction passes through (objective #1)."""

    async def create_customer(self, *, admin_id: uuid.UUID, email: str) -> str:
        """Create (or return) a Stripe customer id for the tenant."""
        ...

    async def create_checkout_session(
        self, *, customer_id: str, success_url: str, cancel_url: str
    ) -> CheckoutSession:
        """Hosted Checkout to add a card — NO charge at save (CJ §6)."""
        ...

    async def ensure_metered_subscription(self, *, customer_id: str) -> str | None:
        """Return the customer's PAYG metered subscription id, creating it if absent.

        Still required under Billing Meters: the meter accumulates the usage, but it is
        the subscription on the meter-backed price that turns that usage into an invoice
        line. Called the first time a tenant has billable overage (§3.4.1b). Returns None
        when the platform has no metered price bound — the caller then reports nothing
        rather than guessing at a price."""
        ...

    async def report_meter_event(self, *, customer_id: str, value: int, identifier: str) -> bool:
        """Post ``value`` blocks to the :data:`METER_EVENT_NAME` meter for the customer.

        Meter events are ADDITIVE — Stripe sums every event in the period — so ``value``
        is the DELTA since the last accepted report, never the period total. Posting a
        cumulative total here would invoice 1+2+3 = 6 blocks for 3 blocks of usage.

        Idempotent on ``identifier`` (HARD CONSTRAINT #3, §3.4.1b): Stripe rejects a
        second event carrying an identifier it has already accepted, which is what makes
        a retry after a timeout safe. Returns True when this call reported new usage,
        False when it was deduped — both mean Stripe now holds the value, so the caller
        may advance its watermark. A raise means it may not."""
        ...

    async def get_payment_method(self, *, customer_id: str) -> PaymentMethodDisplay | None:
        """Return the display tuple for the customer's default card, or None."""
        ...

    async def remove_payment_method(self, *, customer_id: str) -> bool:
        """Detach the card from the customer. Idempotent. Returns True if removed."""
        ...

    def verify_and_parse(self, *, payload: bytes, signature_header: str) -> StripeEvent:
        """Verify the Stripe-Signature header THEN parse (HARD CONSTRAINT #4).
        Raises :class:`SignatureError` on an unverified/garbled signature."""
        ...


def _compute_signature(secret: str, timestamp: str, payload: bytes) -> str:
    """Stripe's v1 scheme: HMAC-SHA256 over ``f"{timestamp}.{payload}"`` (hex)."""
    signed = f"{timestamp}.".encode() + payload
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def build_signature_header(secret: str, payload: bytes, timestamp: int | None = None) -> str:
    """Construct a valid ``Stripe-Signature`` header for `payload` (test/seam helper).

    Mirrors Stripe's wire format ``t=<ts>,v1=<hex>`` so the fake — and the real
    impl — verify identically. Used by tests to forge a correctly-signed webhook
    against the deterministic fake's secret; never makes a network call."""
    ts = timestamp if timestamp is not None else int(time.time())
    return f"t={ts},v1={_compute_signature(secret, str(ts), payload)}"


SIGNATURE_TOLERANCE_SECONDS = 300
"""How stale a signed Stripe event may be. Stripe's own recommended default.

Without an age check the ``t=`` value is parsed and thrown away, so a captured event stays
replayable forever — and every replay of ``invoice.paid`` wipes the usage counter."""


def _verify_and_parse(
    *, secret: str, payload: bytes, signature_header: str, now: float | None = None
) -> StripeEvent:
    """Verify the Stripe-Signature header THEN parse (HARD CONSTRAINT #4).

    Shared by both gateways so the fake cannot drift from the real verification — the
    fake is what the whole suite proves webhook handling against.
    """
    ts, sigs = _parse_signature_header(signature_header)
    if ts is None or not sigs:
        raise SignatureError("Missing Stripe-Signature components.")
    try:
        age = (now if now is not None else time.time()) - int(ts)
    except ValueError as exc:
        raise SignatureError("Malformed Stripe-Signature timestamp.") from exc
    if abs(age) > SIGNATURE_TOLERANCE_SECONDS:
        raise SignatureError("Stripe-Signature timestamp outside tolerance.")
    expected = _compute_signature(secret, ts, payload)
    if not any(hmac.compare_digest(expected, s) for s in sigs):
        raise SignatureError("Stripe signature mismatch.")
    body = json.loads(payload.decode("utf-8"))
    return StripeEvent(
        event_id=str(body["id"]),
        type=str(body["type"]),
        data=dict(body.get("data", {}).get("object", {})),
    )


def _parse_signature_header(header: str) -> tuple[str | None, list[str]]:
    """Split ``t=...,v1=...,v1=...`` into (timestamp, [v1 signatures])."""
    ts: str | None = None
    v1: list[str] = []
    for part in (header or "").split(","):
        key, _, value = part.partition("=")
        if key == "t":
            ts = value
        elif key == "v1":
            v1.append(value)
    return ts, v1


def _is_duplicate_meter_event(exc: BaseException) -> bool:
    """True when Stripe rejected a meter event because it already has that identifier.

    Matched on the message because stripe-python surfaces this as a generic
    ``InvalidRequestError`` with no dedicated error code to key on. Deliberately narrow:
    anything that is not recognizably the duplicate rejection must propagate, because
    swallowing a real failure here would advance the watermark past usage Stripe never
    received — silently un-billing it.
    """
    return "already exists" in str(exc).lower()


class FakeStripeGateway:
    """Deterministic in-process Stripe fake used by ALL tests (objective #1).

    NO network. Customer/PM/usage state lives in-memory keyed by Stripe ids we mint
    deterministically. The webhook signing secret is a fixed test constant so tests
    can forge a correctly-signed event with :func:`build_signature_header`.
    """

    # Test-only signing secret — NOT a real Stripe secret (no live keys in code,
    # config §3.4). detect-secrets pragma: this is a deterministic fixture value.
    WEBHOOK_SECRET = FAKE_STRIPE_WEBHOOK_SECRET

    def __init__(self, *, webhook_secret: str | None = None) -> None:
        # customer_id -> display tuple (or None when no card attached).
        self._cards: dict[str, PaymentMethodDisplay | None] = {}
        # Meter-event identifiers Stripe has already accepted (dedup ledger so retries
        # never double-bill). Additive events make this load-bearing: without it a retry
        # after a timeout adds its value a second time.
        self._event_identifiers: set[str] = set()
        # A provisioned secret WINS over the published constant, so an environment that
        # runs the fake gateway but has a real whsec_ in its platform secret still only
        # accepts events Stripe actually signed. Tests pass nothing and keep the constant.
        self._webhook_secret = webhook_secret or self.WEBHOOK_SECRET
        # customer_id -> metered subscription id.
        self._subscriptions: dict[str, str] = {}
        # subscription id -> the metered price it was bound to at creation.
        self._subscription_prices: dict[str, str] = {}
        # Every meter event accepted, in order — the assertion surface for tests that
        # need to prove usage was reported at the right threshold, not just that the
        # call did not raise.
        self.meter_events: list[MeterEvent] = []
        # customer_id -> the blocks Stripe would BILL, i.e. the running SUM of accepted
        # event values. Recording the calls alone cannot catch an over-bill: the
        # per-call values can each be right while the total is wrong.
        # See :meth:`billed_quantity`.
        self._billed_quantity: dict[str, int] = {}

    async def create_customer(self, *, admin_id: uuid.UUID, email: str) -> str:
        # Deterministic id derived from the tenant so repeated calls are stable.
        cid = f"cus_fake_{admin_id.hex[:16]}"
        self._cards.setdefault(cid, None)
        return cid

    async def create_checkout_session(
        self, *, customer_id: str, success_url: str, cancel_url: str
    ) -> CheckoutSession:
        sid = f"cs_test_{uuid.uuid4().hex[:24]}"
        # The fake "attaches" a card on checkout completion deterministically; in
        # the real flow a payment_method.attached webhook drives this. Here we make
        # the card available so get_payment_method reflects a completed checkout.
        self._cards[customer_id] = PaymentMethodDisplay(
            brand="visa", last4="4242", exp_month=12, exp_year=2030
        )
        return CheckoutSession(url=f"https://checkout.stripe.com/c/pay/{sid}", session_id=sid)

    async def ensure_metered_subscription(self, *, customer_id: str) -> str | None:
        # Same price-id gate as RealStripeGateway: a fake that happily minted a
        # subscription with no price bound would report usage in tests on a path that is
        # silent in production, hiding exactly the misconfiguration this gate exists for.
        price_id = payg_price_id()
        if not price_id:
            return None
        # Deterministic id derived from the customer so repeated calls are stable and
        # the mock never mints a second subscription for the same tenant.
        sub_id = f"sub_fake_{customer_id.removeprefix('cus_fake_')}"
        self._subscriptions[customer_id] = sub_id
        self._subscription_prices[sub_id] = price_id
        return sub_id

    async def report_meter_event(self, *, customer_id: str, value: int, identifier: str) -> bool:
        # HARD CONSTRAINT #3 (§3.4.1b): idempotent on the identifier — a retry after a
        # timeout is a no-op, never a second addition to the meter.
        if identifier in self._event_identifiers:
            return False
        self._event_identifiers.add(identifier)
        self.meter_events.append(
            MeterEvent(
                event_name=METER_EVENT_NAME,
                customer_id=customer_id,
                value=value,
                identifier=identifier,
            )
        )
        # Meter semantics: ADDITIVE. Modelling this is what lets a test assert the
        # invoice total rather than merely the sequence of calls.
        self._billed_quantity[customer_id] = self._billed_quantity.get(customer_id, 0) + value
        return True

    def billed_quantity(self, customer_id: str) -> int:
        """The blocks Stripe would invoice for ``customer_id`` this period.

        Keyed on the CUSTOMER, like the meter events themselves — a meter accumulates
        per customer, not per subscription.
        """
        return self._billed_quantity.get(customer_id, 0)

    def subscription_price(self, subscription_id: str) -> str:
        """The meter-backed price the subscription was created against.

        The price no longer appears on the usage event (the meter carries the usage), so
        this is what proves the operator-configured price is actually consumed rather
        than merely validated — a subscription on the wrong price is a wrong invoice.
        """
        return self._subscription_prices.get(subscription_id, "")

    async def get_payment_method(self, *, customer_id: str) -> PaymentMethodDisplay | None:
        return self._cards.get(customer_id)

    async def remove_payment_method(self, *, customer_id: str) -> bool:
        had = self._cards.get(customer_id) is not None
        self._cards[customer_id] = None
        return had  # idempotent: removing twice just returns False the second time

    def verify_and_parse(self, *, payload: bytes, signature_header: str) -> StripeEvent:
        return _verify_and_parse(
            secret=self._webhook_secret, payload=payload, signature_header=signature_header
        )


class RealStripeGateway:
    """Real stripe-python binding, TEST MODE ONLY (Architecture §3.4.1b).

    Bound ONLY when ``config.stripe_integration_posture == "test"`` (never "live" —
    that value/binding does not exist here). Keys are read from the platform secret
    blob (``LUCIEL_PLATFORM_SECRET``, the same JSON-blob-from-Secrets-Manager shape
    ``app.runtime.provider_factory`` uses for the LLM keys) under:
      * ``STRIPE_TEST_SECRET_KEY``      (sk_test_...)
      * ``STRIPE_TEST_PUBLISHABLE_KEY`` (pk_test_...) — handed to the frontend only
      * ``STRIPE_TEST_WEBHOOK_SECRET``  (whsec_...)   — verifies Stripe-Signature

    NEVER hardcode a key literal here — construction fails loudly (RuntimeError,
    naming only the missing key names, never a value) if a required key is absent,
    mirroring ``provider_factory._build_live_router``'s fail-closed discipline.
    The ``stripe`` package itself is imported LAZILY so CI (no stripe-python
    installed, no network) still imports this module cleanly — it is never invoked
    by the test suite, which always injects :class:`FakeStripeGateway`.

    ``verify_and_parse`` uses the SAME v1 HMAC-SHA256 scheme as the fake (Stripe's
    real wire format), keyed by ``STRIPE_TEST_WEBHOOK_SECRET`` — the webhook route's
    verify-before-route discipline (HARD CONSTRAINT #4) is identical for both.
    """

    def __init__(self, *, secret_key: str, publishable_key: str, webhook_secret: str) -> None:
        if not secret_key.startswith("sk_test_"):
            # Hard fail-closed: refuse anything that is not a recognizable TEST key
            # (in particular, this rejects an sk_live_ key outright — objective
            # DO-NOT — without ever logging the value itself).
            raise RuntimeError(
                "RealStripeGateway requires a Stripe TEST secret key (sk_test_...); "
                "refusing to bind a non-test-mode key."
            )
        self._secret_key = secret_key
        self._publishable_key = publishable_key
        self._webhook_secret = webhook_secret
        self._client: object | None = None

    def _get_client(self) -> object:
        if self._client is None:
            import stripe  # noqa: PLC0415 — lazy/guarded import (CI has no stripe-python)

            stripe.api_key = self._secret_key
            self._client = stripe
        return self._client

    async def create_customer(self, *, admin_id: uuid.UUID, email: str) -> str:
        client = self._get_client()
        customer = client.Customer.create(  # type: ignore[attr-defined]
            email=email, metadata={"admin_id": str(admin_id)}
        )
        return str(customer["id"])

    async def create_checkout_session(
        self, *, customer_id: str, success_url: str, cancel_url: str
    ) -> CheckoutSession:
        client = self._get_client()
        session = client.checkout.Session.create(  # type: ignore[attr-defined]
            customer=customer_id,
            mode="setup",  # add-a-card, NO charge at save (CJ §6)
            payment_method_types=["card"],
            success_url=success_url,
            cancel_url=cancel_url,
        )
        return CheckoutSession(url=str(session["url"]), session_id=str(session["id"]))

    async def ensure_metered_subscription(self, *, customer_id: str) -> str | None:
        """Find or create the customer's PAYG meter-backed subscription.

        The meter accumulates the usage; this subscription is what invoices it, so it is
        still required. ``payg_price_id()`` must name a price backed by the
        :data:`METER_EVENT_NAME` meter — no quantity is set on the item, because the
        meter supplies it.

        With no metered price configured we return None and the caller reports nothing
        rather than creating a subscription against a guessed price (a wrong price is
        worse than no bill — it is an incorrect charge).
        """
        price_id = payg_price_id()
        if not price_id:
            return None
        client = self._get_client()
        existing = client.Subscription.list(customer=customer_id, status="active", limit=1)  # type: ignore[attr-defined]
        items = existing["data"] if "data" in existing else []
        if items:
            return str(items[0]["id"])
        created = client.Subscription.create(  # type: ignore[attr-defined]
            customer=customer_id,
            items=[{"price": price_id}],
            # Idempotent per customer so a retry cannot create a second subscription.
            idempotency_key=f"payg-sub:{customer_id}",
        )
        return str(created["id"])

    async def report_meter_event(self, *, customer_id: str, value: int, identifier: str) -> bool:
        """POST /v1/billing/meter_events — the only usage API that still exists.

        This used to post to the usage-records endpoint on the subscription item, with
        an explicit ``action="set"`` because the value sent was a period-cumulative
        total. That endpoint is REMOVED as of API version 2025-03-31 "basil", which the
        platform account is pinned to: a metered price must now be backed by a Billing
        Meter (creating one without a meter is refused outright), and usage is reported
        as meter events. The old call could therefore never have billed anything — the
        "test" posture was broken for as long as it stood.
        """
        client = self._get_client()
        try:
            client.billing.MeterEvent.create(  # type: ignore[attr-defined]
                event_name=METER_EVENT_NAME,
                # Stripe requires payload values to be strings, including the numeric one.
                payload={"stripe_customer_id": customer_id, "value": str(value)},
                identifier=identifier,
            )
        except Exception as exc:  # noqa: BLE001 — narrowed by the predicate below
            if not _is_duplicate_meter_event(exc):
                raise
            # Stripe already holds this event. Reporting it as a dedup rather than a
            # failure is what lets the caller advance its watermark: treating it as a
            # failure would retry the same spent identifier forever and every later
            # block would go unbilled behind it.
            return False
        return True

    async def get_payment_method(self, *, customer_id: str) -> PaymentMethodDisplay | None:
        client = self._get_client()
        customer = client.Customer.retrieve(customer_id)  # type: ignore[attr-defined]
        # stripe-python returns StripeObject (subscriptable) not dict: use subscript
        # access with guards, NOT nested .get() (which AttributeErrors on the real
        # object — the fake/mock used plain dicts, hiding this in the suite).
        inv = customer["invoice_settings"] if "invoice_settings" in customer else None
        pm_id = inv["default_payment_method"] if inv and "default_payment_method" in inv else None
        if not pm_id:
            return None
        pm = client.PaymentMethod.retrieve(pm_id)  # type: ignore[attr-defined]
        card = pm["card"] if "card" in pm else None
        if not card:
            return None
        return PaymentMethodDisplay(
            brand=str(card["brand"]),
            last4=str(card["last4"]),
            exp_month=int(card["exp_month"]),
            exp_year=int(card["exp_year"]),
        )

    async def remove_payment_method(self, *, customer_id: str) -> bool:
        client = self._get_client()
        customer = client.Customer.retrieve(customer_id)  # type: ignore[attr-defined]
        inv = customer["invoice_settings"] if "invoice_settings" in customer else None
        pm_id = inv["default_payment_method"] if inv and "default_payment_method" in inv else None
        if not pm_id:
            return False
        client.PaymentMethod.detach(pm_id)  # type: ignore[attr-defined]
        return True

    def verify_and_parse(self, *, payload: bytes, signature_header: str) -> StripeEvent:
        # Same v1 scheme as the fake, keyed by the TEST webhook signing secret.
        return _verify_and_parse(
            secret=self._webhook_secret, payload=payload, signature_header=signature_header
        )


def _build_test_gateway() -> RealStripeGateway:
    """Build the TEST-posture RealStripeGateway from the platform secret blob.

    Fails loudly (RuntimeError naming only the missing key names — NEVER a value)
    if a required TEST key is absent, mirroring
    ``app.runtime.provider_factory._build_live_router``'s fail-closed discipline.
    """
    from app.config import get_settings, parse_platform_secret  # noqa: PLC0415

    settings = get_settings()
    secret = parse_platform_secret(settings.platform_secret, live=True)
    secret_key = secret.get("STRIPE_TEST_SECRET_KEY", "")
    publishable_key = secret.get("STRIPE_TEST_PUBLISHABLE_KEY", "")
    webhook_secret = secret.get("STRIPE_TEST_WEBHOOK_SECRET", "")
    missing = [
        n
        for n, v in (
            ("STRIPE_TEST_SECRET_KEY", secret_key),
            ("STRIPE_TEST_PUBLISHABLE_KEY", publishable_key),
            ("STRIPE_TEST_WEBHOOK_SECRET", webhook_secret),
        )
        if not v
    ]
    if missing:
        raise RuntimeError(
            "stripe_integration_posture=test but the platform secret is missing keys: "
            f"{', '.join(missing)}"
        )
    return RealStripeGateway(
        secret_key=secret_key, publishable_key=publishable_key, webhook_secret=webhook_secret
    )


_gateway: StripeGateway | None = None


def get_stripe_gateway() -> StripeGateway:
    """Return the process gateway singleton (the fake by default).

    ``stripe_integration_posture`` gates the binding exactly like the LLM GATE 2
    cutover: "mock" (default) → :class:`FakeStripeGateway`, no keys, no network —
    every test stays on this path. "test" → :class:`RealStripeGateway` bound
    against the Stripe TEST keys (never sk_live_). This is the swappable seam —
    flagged per the objective."""
    global _gateway
    if _gateway is None:
        from app.config import get_settings  # noqa: PLC0415 — avoid import cycle

        settings = get_settings()
        if settings.stripe_integration_posture == "test":
            _gateway = _build_test_gateway()
        else:
            _gateway = FakeStripeGateway(webhook_secret=settings.stripe_webhook_secret or None)
    return _gateway


def set_stripe_gateway(gateway: StripeGateway | None) -> None:
    """Override the gateway (tests inject a fresh fake; None resets to default)."""
    global _gateway
    _gateway = gateway
