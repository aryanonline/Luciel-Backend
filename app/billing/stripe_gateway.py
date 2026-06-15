"""Stripe gateway seam (Architecture §3.4.1b — billing/metering).

ALL Stripe interaction goes through the :class:`StripeGateway` Protocol. This is a
swappable seam (HARD CONSTRAINT, Arc 18 objective #1):

  * CI / every test uses :class:`FakeStripeGateway` — a DETERMINISTIC, in-process
    fake. There is NO real Stripe network call anywhere in the test path.
  * A real stripe-python TEST-MODE impl (:class:`RealStripeGateway`, below) may
    bind behind the same interface, but is NOT invoked by tests and requires a
    Stripe secret key that is deliberately ABSENT in CI. It is gated on owner
    approval (infra-deferred seam, mirrors the connections OAuth/AWS seam).
  * NO live/production Stripe — ever (objective DO-NOT).

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

    async def report_metered_usage(
        self, *, subscription_id: str, quantity: int, idempotency_key: str
    ) -> bool:
        """Report metered usage for the period. Idempotent on ``idempotency_key``
        (HARD CONSTRAINT #3, §3.4.1b): a retry with the same key never double-bills.
        Returns True if this call reported new usage, False if it was a dedup no-op."""
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


class FakeStripeGateway:
    """Deterministic in-process Stripe fake used by ALL tests (objective #1).

    NO network. Customer/PM/usage state lives in-memory keyed by Stripe ids we mint
    deterministically. The webhook signing secret is a fixed test constant so tests
    can forge a correctly-signed event with :func:`build_signature_header`.
    """

    # Test-only signing secret — NOT a real Stripe secret (no live keys in code,
    # config §3.4). detect-secrets pragma: this is a deterministic fixture value.
    WEBHOOK_SECRET = "whsec_fake_test_only"  # noqa: S105 # nosec B105

    def __init__(self) -> None:
        # customer_id -> display tuple (or None when no card attached).
        self._cards: dict[str, PaymentMethodDisplay | None] = {}
        # idempotency_key -> reported (dedup ledger so retries never double-bill).
        self._usage_keys: set[str] = set()

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

    async def report_metered_usage(
        self, *, subscription_id: str, quantity: int, idempotency_key: str
    ) -> bool:
        # HARD CONSTRAINT #3 (§3.4.1b): idempotent on the key — a redelivery / retry
        # with the same (period, quantity) key is a no-op, never a double-bill.
        if idempotency_key in self._usage_keys:
            return False
        self._usage_keys.add(idempotency_key)
        return True

    async def get_payment_method(self, *, customer_id: str) -> PaymentMethodDisplay | None:
        return self._cards.get(customer_id)

    async def remove_payment_method(self, *, customer_id: str) -> bool:
        had = self._cards.get(customer_id) is not None
        self._cards[customer_id] = None
        return had  # idempotent: removing twice just returns False the second time

    def verify_and_parse(self, *, payload: bytes, signature_header: str) -> StripeEvent:
        # HARD CONSTRAINT #4: verify BEFORE parsing/acting (Arc 13 discipline).
        ts, sigs = _parse_signature_header(signature_header)
        if ts is None or not sigs:
            raise SignatureError("Missing Stripe-Signature components.")
        expected = _compute_signature(self.WEBHOOK_SECRET, ts, payload)
        if not any(hmac.compare_digest(expected, s) for s in sigs):
            raise SignatureError("Stripe signature mismatch.")
        body = json.loads(payload.decode("utf-8"))
        return StripeEvent(
            event_id=str(body["id"]),
            type=str(body["type"]),
            data=dict(body.get("data", {}).get("object", {})),
        )


_gateway: StripeGateway | None = None


def get_stripe_gateway() -> StripeGateway:
    """Return the process gateway singleton (the fake by default).

    The real stripe-python TEST-MODE binding (:class:`RealStripeGateway`) installs
    here on owner approval when a Stripe key is present; CI leaves the fake. This
    is the swappable seam — flagged per the objective."""
    global _gateway
    if _gateway is None:
        _gateway = FakeStripeGateway()
    return _gateway


def set_stripe_gateway(gateway: StripeGateway | None) -> None:
    """Override the gateway (tests inject a fresh fake; None resets to default)."""
    global _gateway
    _gateway = gateway
