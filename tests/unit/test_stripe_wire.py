"""RealStripeGateway on the wire: the REAL stripe-python builds every request.

The other RealStripeGateway tests swap the ``stripe`` module for a hand-written object,
which cannot notice the library or the Stripe API moving underneath it. stripe-python
16.0.0 moved the API version it sends to 2026-09-30.endive, which removed
``payment_method_types`` from Checkout Session create (Stripe answers 400), and those
fakes kept accepting it. Here the real library encodes each request, parses each
response and turns each error status into its exception; only the transport is
replaced, by an in-process recorder. NO network, and the key is a dummy.

The responses are written here, so this proves what we send and how the SDK hands
Stripe's answers back to the gateway, not how Stripe answers. An error message marked
SYNTHETIC is our wording, not Stripe's.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest
import stripe
from app.billing.stripe_gateway import (
    METER_EVENT_NAME,
    STRIPE_API_VERSION,
    PaymentMethodDisplay,
    RealStripeGateway,
)
from app.config import get_settings
from requests.structures import CaseInsensitiveDict
from stripe import HTTPClient

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "provision_stripe_test.py"

Responses = dict[tuple[str, str], tuple[int, dict[str, Any]]]
# (method, path, query, form): one request as the library put it on the wire.
Sent = tuple[str, str, dict[str, str], dict[str, str]]


class _Unrouted(BaseException):
    """A request no route answers. A BaseException, so the gateway's own ``except
    Exception`` handlers (gone objects, duplicate meter events) cannot swallow it."""


class _Recorder(HTTPClient):
    """stripe-python's transport, and nothing else: answers from ``responses`` (keyed on
    method + path) and records what the library actually put on the wire."""

    name = "recorder"

    def __init__(self, responses: Responses) -> None:
        super().__init__()
        self.responses = responses
        self.requests: list[dict[str, Any]] = []

    def request(self, method, url, headers, post_data=None, *, _usage=None):
        parts = urlsplit(url)
        body = post_data.decode() if isinstance(post_data, bytes) else (post_data or "")
        route = (method.upper(), parts.path)
        self.requests.append(
            {
                "method": route[0],
                "path": route[1],
                "query": dict(parse_qsl(parts.query, keep_blank_values=True)),
                "form": dict(parse_qsl(body, keep_blank_values=True)),
                "stripe_version": headers.get("Stripe-Version"),
                "idempotency_key": headers.get("Idempotency-Key"),
            }
        )
        if route not in self.responses:
            raise _Unrouted(f"no response for {route[0]} {route[1]}")
        status, payload = self.responses[route]
        # Case-insensitive, like a real transport's headers: the SDK reads both spellings.
        return json.dumps(payload), status, CaseInsensitiveDict({"Request-Id": "req_wire"})

    def close(self) -> None:
        pass

    def sent(self) -> list[Sent]:
        """Every request in order: the exact wire transcript."""
        return [(r["method"], r["path"], r["query"], r["form"]) for r in self.requests]


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch):
    """Install a recorder as stripe-python's transport; every global is restored after.

    Installing again replaces the transport and resets the version, so build a new
    gateway after it: a gateway sets the version once, on its first request."""

    def install(responses: Responses) -> _Recorder:
        recorder = _Recorder(responses)
        monkeypatch.setattr(stripe, "default_http_client", recorder)
        monkeypatch.setattr(stripe, "api_key", None)
        monkeypatch.setattr(stripe, "max_network_retries", 0)
        # Anything but the pin: the gateway must SET the version, not inherit whatever
        # the installed SDK (or an earlier caller) left in the module.
        monkeypatch.setattr(stripe, "api_version", "2099-01-01.not-the-pin")
        return recorder

    return install


def _gateway() -> RealStripeGateway:
    # Dummy test-mode-shaped values; the recorder never sends them anywhere.
    return RealStripeGateway(
        secret_key="sk_test_wire",  # pragma: allowlist secret
        webhook_secret="whsec_wire",  # pragma: allowlist secret
    )


def _list(url: str, data: list[dict[str, Any]]) -> dict[str, Any]:
    return {"object": "list", "data": data, "has_more": False, "url": url}


def _stripe_error(status: int, message: str, *, code: str | None = None) -> tuple[int, dict]:
    """A response shaped like a Stripe error: a 4xx is an ``invalid_request_error``,
    a 5xx an ``api_error``."""
    error: dict[str, Any] = {
        "type": "invalid_request_error" if status < 500 else "api_error",
        "message": message,
    }
    if code is not None:
        error["code"] = code
    return status, {"error": error}


def _no_such(kind: str, object_id: str) -> tuple[int, dict]:
    """Shaped like Stripe's ``resource_missing`` answer for an id it does not know."""
    return _stripe_error(404, f"No such {kind}: '{object_id}'", code="resource_missing")


_VISA = {"brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2030}
_MASTERCARD = {"brand": "mastercard", "last4": "4444", "exp_month": 1, "exp_year": 2031}


def test_checkout_session_goes_out_card_only_on_the_pinned_api_version(wire) -> None:
    """The add-a-card session stays card-only through ``allowed_payment_method_types``,
    the endive replacement for the removed ``payment_method_types``; with that filter
    set, Stripe needs no ``currency`` in setup mode."""
    recorder = wire(
        {
            ("POST", "/v1/checkout/sessions"): (
                200,
                {
                    "id": "cs_test_wire",
                    "object": "checkout.session",
                    "mode": "setup",
                    "url": "https://checkout.stripe.com/c/pay/cs_test_wire",
                },
            )
        }
    )

    session = asyncio.run(
        _gateway().create_checkout_session(
            customer_id="cus_wire",
            success_url="https://app.test/ok",
            cancel_url="https://app.test/no",
        )
    )

    assert (session.url, session.session_id) == (
        "https://checkout.stripe.com/c/pay/cs_test_wire",
        "cs_test_wire",
    )
    [sent] = recorder.requests
    assert (sent["method"], sent["path"]) == ("POST", "/v1/checkout/sessions")
    assert sent["stripe_version"] == STRIPE_API_VERSION
    # Exact: a payment_method_types[0] here is the 400 this pins against.
    assert sent["form"] == {
        "customer": "cus_wire",
        "mode": "setup",
        "allowed_payment_method_types[0]": "card",
        "success_url": "https://app.test/ok",
        "cancel_url": "https://app.test/no",
    }


def test_each_gateway_method_sends_its_exact_request_on_the_pinned_api_version(
    wire, monkeypatch
) -> None:
    """Every method's success path, as the exact transcript of what the library sends,
    and the shapes the gateway reads back: a StripeObject (not a dict) for the card, and
    a subscription whose billing period lives on its items (basil onwards)."""
    monkeypatch.setattr(get_settings(), "stripe_payg_price_id", "price_wire")
    monkeypatch.setattr(get_settings(), "stripe_portal_configuration_id", "bpc_wire")
    item = {
        "id": "si_wire",
        "object": "subscription_item",
        "current_period_start": 1767225600,
        "current_period_end": 1769904000,
    }
    recorder = wire(
        {
            ("POST", "/v1/customers"): (200, {"id": "cus_wire", "object": "customer"}),
            ("POST", "/v1/checkout/sessions"): (
                200,
                {"id": "cs_wire", "object": "checkout.session", "url": "https://checkout"},
            ),
            ("POST", "/v1/billing_portal/sessions"): (
                200,
                {"id": "bps_wire", "object": "billing_portal.session", "url": "https://portal"},
            ),
            ("GET", "/v1/subscriptions"): (200, _list("/v1/subscriptions", [])),
            ("POST", "/v1/subscriptions"): (200, {"id": "sub_wire", "object": "subscription"}),
            ("POST", "/v1/billing/meter_events"): (
                200,
                {"object": "billing.meter_event", "identifier": "u:1-3"},
            ),
            ("GET", "/v1/customers/cus_wire"): (
                200,
                {
                    "id": "cus_wire",
                    "object": "customer",
                    "invoice_settings": {"default_payment_method": "pm_wire"},
                },
            ),
            ("GET", "/v1/payment_methods/pm_wire"): (
                200,
                {"id": "pm_wire", "object": "payment_method", "card": _VISA},
            ),
            ("POST", "/v1/payment_methods/pm_wire/detach"): (
                200,
                {"id": "pm_wire", "object": "payment_method"},
            ),
            ("POST", "/v1/customers/cus_wire"): (200, {"id": "cus_wire", "object": "customer"}),
            ("GET", "/v1/subscriptions/sub_wire"): (
                200,
                {
                    "id": "sub_wire",
                    "object": "subscription",
                    "items": _list("/v1/subscription_items", [item]),
                },
            ),
            ("DELETE", "/v1/subscriptions/sub_wire"): (
                200,
                {"id": "sub_wire", "object": "subscription", "status": "canceled"},
            ),
            ("DELETE", "/v1/customers/cus_wire"): (
                200,
                {"id": "cus_wire", "object": "customer", "deleted": True},
            ),
        }
    )
    gw = _gateway()

    async def run() -> tuple[Any, ...]:
        await gw.create_customer(admin_id=uuid.UUID(int=1), email="owner@example.test")
        await gw.create_checkout_session(
            customer_id="cus_wire", success_url="https://ok", cancel_url="https://no"
        )
        await gw.create_portal_session(customer_id="cus_wire", return_url="https://back")
        return (
            await gw.ensure_metered_subscription(customer_id="cus_wire"),
            await gw.report_meter_event(customer_id="cus_wire", value=2, identifier="u:1-3"),
            await gw.get_payment_method(customer_id="cus_wire"),
            await gw.remove_payment_method(customer_id="cus_wire"),
            await gw.set_default_payment_method(
                customer_id="cus_wire", payment_method_id="pm_wire"
            ),
            await gw.get_subscription_period(subscription_id="sub_wire"),
            await gw.cancel_subscription(subscription_id="sub_wire", invoice_now=True),
            await gw.delete_customer(customer_id="cus_wire"),
        )

    sub, reported, on_file, removed, defaulted, period, cancelled, deleted = asyncio.run(run())

    assert (sub, reported, removed, defaulted, cancelled, deleted) == (
        "sub_wire",
        True,
        True,
        True,
        True,
        True,
    )
    assert on_file == PaymentMethodDisplay(brand="visa", last4="4242", exp_month=12, exp_year=2030)
    assert period == (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 2, 1, tzinfo=UTC))
    assert recorder.sent() == [
        (
            "POST",
            "/v1/customers",
            {},
            {"email": "owner@example.test", "metadata[admin_id]": str(uuid.UUID(int=1))},
        ),
        (
            "POST",
            "/v1/checkout/sessions",
            {},
            {
                "customer": "cus_wire",
                "mode": "setup",
                "allowed_payment_method_types[0]": "card",
                "success_url": "https://ok",
                "cancel_url": "https://no",
            },
        ),
        (
            "POST",
            "/v1/billing_portal/sessions",
            {},
            {"customer": "cus_wire", "return_url": "https://back", "configuration": "bpc_wire"},
        ),
        (
            "GET",
            "/v1/subscriptions",
            {"customer": "cus_wire", "status": "active", "limit": "1"},
            {},
        ),
        (
            "POST",
            "/v1/subscriptions",
            {},
            {"customer": "cus_wire", "items[0][price]": "price_wire"},
        ),
        (
            "POST",
            "/v1/billing/meter_events",
            {},
            {
                "event_name": METER_EVENT_NAME,
                "payload[stripe_customer_id]": "cus_wire",
                "payload[value]": "2",
                "identifier": "u:1-3",
            },
        ),
        ("GET", "/v1/customers/cus_wire", {}, {}),
        ("GET", "/v1/payment_methods/pm_wire", {}, {}),
        ("GET", "/v1/customers/cus_wire", {}, {}),
        ("POST", "/v1/payment_methods/pm_wire/detach", {}, {}),
        (
            "POST",
            "/v1/customers/cus_wire",
            {},
            {"invoice_settings[default_payment_method]": "pm_wire"},
        ),
        ("GET", "/v1/subscriptions/sub_wire", {}, {}),
        ("DELETE", "/v1/subscriptions/sub_wire", {"invoice_now": "true", "prorate": "false"}, {}),
        ("DELETE", "/v1/customers/cus_wire", {}, {}),
    ]
    assert {r["stripe_version"] for r in recorder.requests} == {STRIPE_API_VERSION}
    # Ours, unique per attempt (ensure_metered_subscription), not one the SDK made up.
    [create] = [
        r for r in recorder.requests if (r["method"], r["path"]) == ("POST", "/v1/subscriptions")
    ]
    assert re.fullmatch(r"payg-sub:cus_wire:[0-9a-f]{32}", create["idempotency_key"])


def test_an_active_subscription_is_reused_not_created_again(wire, monkeypatch) -> None:
    """The idempotency key is unique per attempt, so this lookup is the only thing that
    keeps a retry from minting a second metered subscription: one read, the existing id
    back, and no create."""
    monkeypatch.setattr(get_settings(), "stripe_payg_price_id", "price_wire")
    active = {"id": "sub_live", "object": "subscription", "status": "active"}
    recorder = wire({("GET", "/v1/subscriptions"): (200, _list("/v1/subscriptions", [active]))})

    sub = asyncio.run(_gateway().ensure_metered_subscription(customer_id="cus_wire"))

    assert sub == "sub_live"
    assert recorder.sent() == [
        ("GET", "/v1/subscriptions", {"customer": "cus_wire", "status": "active", "limit": "1"}, {})
    ]


def test_without_an_invoice_default_the_newest_card_shows_and_every_card_detaches(
    wire,
) -> None:
    """Checkout in setup mode attaches a card without making it the invoice default
    (F006). Until the attached webhook does that, the card on file is the newest attached
    one, and "remove my card" detaches every card rather than doing nothing."""
    recorder = wire(
        {
            ("GET", "/v1/customers/cus_wire"): (
                200,
                {
                    "id": "cus_wire",
                    "object": "customer",
                    "invoice_settings": {"default_payment_method": None},
                },
            ),
            # Newest first, as Stripe lists them (the recorder ignores ``limit``).
            ("GET", "/v1/payment_methods"): (
                200,
                _list(
                    "/v1/payment_methods",
                    [
                        {"id": "pm_new", "object": "payment_method", "card": _VISA},
                        {"id": "pm_old", "object": "payment_method", "card": _MASTERCARD},
                    ],
                ),
            ),
            ("POST", "/v1/payment_methods/pm_new/detach"): (
                200,
                {"id": "pm_new", "object": "payment_method"},
            ),
            ("POST", "/v1/payment_methods/pm_old/detach"): (
                200,
                {"id": "pm_old", "object": "payment_method"},
            ),
        }
    )
    gw = _gateway()

    on_file = asyncio.run(gw.get_payment_method(customer_id="cus_wire"))
    removed = asyncio.run(gw.remove_payment_method(customer_id="cus_wire"))

    assert on_file == PaymentMethodDisplay(brand="visa", last4="4242", exp_month=12, exp_year=2030)
    assert removed is True
    assert recorder.sent() == [
        ("GET", "/v1/customers/cus_wire", {}, {}),
        ("GET", "/v1/payment_methods", {"customer": "cus_wire", "type": "card", "limit": "1"}, {}),
        ("GET", "/v1/customers/cus_wire", {}, {}),
        ("GET", "/v1/payment_methods", {"customer": "cus_wire", "type": "card"}, {}),
        ("POST", "/v1/payment_methods/pm_new/detach", {}, {}),
        ("POST", "/v1/payment_methods/pm_old/detach", {}, {}),
    ]
    assert {r["stripe_version"] for r in recorder.requests} == {STRIPE_API_VERSION}


def test_with_no_card_on_file_there_is_nothing_to_show_or_detach(wire) -> None:
    """No default and no attached card: no display tuple, and removal reports False
    without a detach."""
    recorder = wire(
        {
            ("GET", "/v1/customers/cus_wire"): (
                200,
                {
                    "id": "cus_wire",
                    "object": "customer",
                    "invoice_settings": {"default_payment_method": None},
                },
            ),
            ("GET", "/v1/payment_methods"): (200, _list("/v1/payment_methods", [])),
        }
    )
    gw = _gateway()

    assert asyncio.run(gw.get_payment_method(customer_id="cus_wire")) is None
    assert asyncio.run(gw.remove_payment_method(customer_id="cus_wire")) is False

    assert [(method, path) for method, path, _, _ in recorder.sent()] == [
        ("GET", "/v1/customers/cus_wire"),
        ("GET", "/v1/payment_methods"),
        ("GET", "/v1/customers/cus_wire"),
        ("GET", "/v1/payment_methods"),
    ]


def test_a_gone_subscription_cancels_as_a_no_op_but_other_refusals_raise(wire) -> None:
    """``cancel_subscription`` reads Stripe's message off the SDK's exception. The real
    library turns the 404 into an InvalidRequestError whose str() carries "No such
    subscription", so a second cancel is a no-op; any other refusal must still raise."""
    recorder = wire(
        {
            ("DELETE", "/v1/subscriptions/sub_gone"): _no_such("subscription", "sub_gone"),
            ("DELETE", "/v1/subscriptions/sub_held"): _stripe_error(
                400, "SYNTHETIC: refused for some other reason"
            ),
        }
    )
    gw = _gateway()

    cancelled = asyncio.run(gw.cancel_subscription(subscription_id="sub_gone", invoice_now=True))

    assert cancelled is False
    with pytest.raises(stripe.InvalidRequestError, match="SYNTHETIC"):
        asyncio.run(gw.cancel_subscription(subscription_id="sub_held", invoice_now=False))

    assert recorder.sent() == [
        ("DELETE", "/v1/subscriptions/sub_gone", {"invoice_now": "true", "prorate": "false"}, {}),
        ("DELETE", "/v1/subscriptions/sub_held", {"invoice_now": "false", "prorate": "false"}, {}),
    ]


def test_a_gone_customer_deletes_as_a_no_op_but_a_server_failure_raises(wire) -> None:
    recorder = wire(
        {
            ("DELETE", "/v1/customers/cus_gone"): _no_such("customer", "cus_gone"),
            ("DELETE", "/v1/customers/cus_down"): _stripe_error(500, "SYNTHETIC: server failure"),
        }
    )
    gw = _gateway()

    assert asyncio.run(gw.delete_customer(customer_id="cus_gone")) is False
    with pytest.raises(stripe.APIError, match="SYNTHETIC"):
        asyncio.run(gw.delete_customer(customer_id="cus_down"))

    assert recorder.sent() == [
        ("DELETE", "/v1/customers/cus_gone", {}, {}),
        ("DELETE", "/v1/customers/cus_down", {}, {}),
    ]


def test_a_gone_subscription_has_no_period(wire) -> None:
    recorder = wire({("GET", "/v1/subscriptions/sub_gone"): _no_such("subscription", "sub_gone")})

    assert asyncio.run(_gateway().get_subscription_period(subscription_id="sub_gone")) is None

    assert recorder.sent() == [("GET", "/v1/subscriptions/sub_gone", {}, {})]


def test_a_duplicate_meter_event_is_a_dedup_but_a_server_failure_raises(wire) -> None:
    """The real library turns Stripe's 400 into an InvalidRequestError whose str()
    carries the message ``_is_duplicate_meter_event`` matches, so a retried identifier
    advances the watermark instead of retrying forever. The duplicate wording is
    SYNTHETIC: Stripe's own message has not been observed, so this pins the SDK's half
    only. A server failure must raise, or the watermark would pass usage Stripe never
    recorded."""
    duplicate = wire(
        {
            ("POST", "/v1/billing/meter_events"): _stripe_error(
                400, "SYNTHETIC: An event with identifier u:1-3 already exists."
            )
        }
    )
    reported = asyncio.run(
        _gateway().report_meter_event(customer_id="cus_wire", value=2, identifier="u:1-3")
    )
    assert reported is False

    down = wire({("POST", "/v1/billing/meter_events"): _stripe_error(500, "SYNTHETIC: down")})
    with pytest.raises(stripe.APIError, match="SYNTHETIC"):
        asyncio.run(
            _gateway().report_meter_event(customer_id="cus_wire", value=1, identifier="u:4-4")
        )

    for recorder, value, identifier in ((duplicate, "2", "u:1-3"), (down, "1", "u:4-4")):
        assert recorder.sent() == [
            (
                "POST",
                "/v1/billing/meter_events",
                {},
                {
                    "event_name": METER_EVENT_NAME,
                    "payload[stripe_customer_id]": "cus_wire",
                    "payload[value]": value,
                    "identifier": identifier,
                },
            )
        ]
        assert recorder.requests[0]["stripe_version"] == STRIPE_API_VERSION


def test_the_provisioning_script_calls_stripe_on_the_same_pinned_version(
    wire, monkeypatch, capsys
) -> None:
    """The script creates the meter, price and portal configuration the gateway then
    uses, so it must speak the gateway's API version, not the installed SDK's default.
    ``--verify`` is read-only; one listing is enough to read the header."""
    recorder = wire({("GET", "/v1/billing/meters"): (200, _list("/v1/billing/meters", []))})
    monkeypatch.setenv("STRIPE_TEST_SECRET_KEY", "sk_test_wire")
    spec = importlib.util.spec_from_file_location("provision_stripe_test_wire", _SCRIPT)
    assert spec and spec.loader
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)

    assert script.main(["--verify"]) == 1  # the recorder holds no meter

    assert [r["stripe_version"] for r in recorder.requests] == [STRIPE_API_VERSION]
    assert "MISSING: Billing Meter" in capsys.readouterr().out
