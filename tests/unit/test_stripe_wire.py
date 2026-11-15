"""RealStripeGateway on the wire: the REAL stripe-python builds every request.

The other RealStripeGateway tests swap the ``stripe`` module for a hand-written object,
which cannot notice the library or the Stripe API moving underneath it. stripe-python
16.0.0 moved the API version it sends to 2026-09-30.endive, which removed
``payment_method_types`` from Checkout Session create (Stripe answers 400), and those
fakes kept accepting it. Here the real library encodes each request and parses each
response; only the transport is replaced, by an in-process recorder. NO network, and
the key is a dummy.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
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
from stripe import HTTPClient

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "provision_stripe_test.py"

Responses = dict[tuple[str, str], tuple[int, dict[str, Any]]]


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
        self.requests.append(
            {
                "method": method.upper(),
                "path": parts.path,
                "query": dict(parse_qsl(parts.query)),
                "form": dict(parse_qsl(body)),
                "stripe_version": headers.get("Stripe-Version"),
            }
        )
        status, payload = self.responses[(method.upper(), parts.path)]
        return json.dumps(payload), status, {"Request-Id": "req_wire"}

    def close(self) -> None:
        pass


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch):
    """Install a recorder as stripe-python's transport; every global is restored after."""

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


def test_every_gateway_call_goes_out_on_the_pinned_api_version(wire, monkeypatch) -> None:
    """The pin covers every call, and the real library parses the shapes the gateway
    reads: a StripeObject (not a dict) for the card, and a subscription whose billing
    period lives on its items (basil onwards)."""
    monkeypatch.setattr(get_settings(), "stripe_payg_price_id", "price_wire")
    monkeypatch.setattr(get_settings(), "stripe_portal_configuration_id", "bpc_wire")
    card = {"brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2030}
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
                {"id": "pm_wire", "object": "payment_method", "card": card},
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
    assert len(recorder.requests) == 14
    assert {r["stripe_version"] for r in recorder.requests} == {STRIPE_API_VERSION}
    sent = {(r["method"], r["path"]): r for r in recorder.requests}
    assert sent[("POST", "/v1/billing/meter_events")]["form"] == {
        "event_name": METER_EVENT_NAME,
        "payload[stripe_customer_id]": "cus_wire",
        "payload[value]": "2",
        "identifier": "u:1-3",
    }
    assert sent[("POST", "/v1/subscriptions")]["form"] == {
        "customer": "cus_wire",
        "items[0][price]": "price_wire",
    }
    assert sent[("DELETE", "/v1/subscriptions/sub_wire")]["query"] == {
        "invoice_now": "true",
        "prorate": "false",
    }


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
