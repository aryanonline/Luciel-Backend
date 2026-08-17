#!/usr/bin/env python3
"""Provision the Stripe TEST-mode billing objects the platform expects.

WHY THIS EXISTS: the metering layer is complete and test-pinned, but it hangs off
one config value — ``stripe_payg_price_id`` — whose default is deliberately empty
(``test_payg_price_id_is_empty_by_default``). With no price configured,
``ensure_metered_subscription`` returns None and every billable block is counted
locally but never posted to Stripe (``test_no_configured_price_reports_nothing``).
Until the objects below exist in the Stripe account and the price id reaches the
platform secret, PAYG revenue is a number in Postgres and nothing else.

Provisions (idempotently — a second run creates nothing):
  1. Billing Meter  ``luciel_payg_blocks``  (sum over payload key "value",
     customer-mapped by payload key "stripe_customer_id") — MUST match the shape
     ``RealStripeGateway.report_meter_event`` posts, which is why the event name
     is imported from the gateway rather than restated.
  2. Product        "Luciel conversations — pay-as-you-go block of 100"
     (found again via ``metadata.luciel_key = payg_block``).
  3. Price          3900¢ CAD / month, metered, backed by the meter
     (found again via ``metadata.luciel_key = payg_block_3900``). CAD is the
     owner-confirmed billing currency (2026-08-17): Canadian residency, CASL,
     Ontario CPA — the $39 in the docs is Canadian dollars.

The price's numbers are read from ``app.config`` (3900¢ per 100-block), so a
pricing change in config makes this script REFUSE a stale price rather than
silently reusing it — a wrong price is an incorrect charge, worse than none.

AUTH: the secret key is read from the ``STRIPE_TEST_SECRET_KEY`` environment
variable — never a flag, never a literal (owner decision 2026-08-17: local env
var handoff). A non-``sk_test_`` key is refused outright with the same posture
as ``RealStripeGateway``: this script must be physically unable to touch a live
Stripe account.

RUN:
    STRIPE_TEST_SECRET_KEY=sk_test_... python scripts/provision_stripe_test.py
    python scripts/provision_stripe_test.py --verify   # read-only re-check

After a successful run, put the printed price id where the platform reads it:
``STRIPE_PAYG_PRICE_ID`` in the ``luciel/<env>/platform`` secret (or the
``LUCIEL_STRIPE_PAYG_PRICE_ID`` env var, which wins over the blob).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.billing.stripe_gateway import METER_EVENT_NAME  # noqa: E402
from app.config import get_settings  # noqa: E402

PRODUCT_KEY = "payg_block"
PRICE_KEY = "payg_block_3900"
CURRENCY = "cad"  # owner-confirmed 2026-08-17; a Stripe price's currency is permanent

_P = "[provision-stripe]"


def _meta(obj: object, key: str) -> str:
    """metadata lookup in the gateway's subscript-with-guard style (StripeObject
    is subscriptable but nested ``.get`` chains AttributeError on the real SDK)."""
    md = obj["metadata"] if "metadata" in obj else None  # type: ignore[index, operator]
    if md is None:
        return ""
    return str(md[key]) if key in md else ""  # type: ignore[index, operator]


def _find_meter(stripe_mod: object) -> object | None:
    listing = stripe_mod.billing.Meter.list(status="active", limit=100)  # type: ignore[attr-defined]
    for meter in listing["data"]:  # type: ignore[index]
        if str(meter["event_name"]) == METER_EVENT_NAME:
            return meter
    return None


def _ensure_meter(stripe_mod: object) -> tuple[object, bool]:
    existing = _find_meter(stripe_mod)
    if existing is not None:
        return existing, False
    created = stripe_mod.billing.Meter.create(  # type: ignore[attr-defined]
        display_name="Luciel PAYG blocks",
        event_name=METER_EVENT_NAME,
        # These three payload bindings ARE the contract with
        # RealStripeGateway.report_meter_event — payload {"stripe_customer_id",
        # "value"} — a mismatch here silently drops every reported block.
        default_aggregation={"formula": "sum"},
        value_settings={"event_payload_key": "value"},
        customer_mapping={"event_payload_key": "stripe_customer_id", "type": "by_id"},
    )
    return created, True


def _find_product(stripe_mod: object) -> object | None:
    listing = stripe_mod.Product.list(active=True, limit=100)  # type: ignore[attr-defined]
    for product in listing["data"]:  # type: ignore[index]
        if _meta(product, "luciel_key") == PRODUCT_KEY:
            return product
    return None


def _ensure_product(stripe_mod: object) -> tuple[object, bool]:
    existing = _find_product(stripe_mod)
    if existing is not None:
        return existing, False
    settings = get_settings()
    created = stripe_mod.Product.create(  # type: ignore[attr-defined]
        name=f"Luciel conversations — pay-as-you-go block of {settings.payg_block_size}",
        metadata={"luciel_key": PRODUCT_KEY},
    )
    return created, True


def _find_price(stripe_mod: object, *, product_id: str) -> object | None:
    listing = stripe_mod.Price.list(product=product_id, active=True, limit=100)  # type: ignore[attr-defined]
    for price in listing["data"]:  # type: ignore[index]
        if _meta(price, "luciel_key") == PRICE_KEY:
            return price
    return None


def _check_price(price: object, *, meter_id: str) -> list[str]:
    """Mismatches between an existing tagged price and the platform's numbers.

    Non-empty means STOP: adopting a price whose currency or amount disagrees
    with config would bill customers wrongly. The remedy is manual on purpose —
    deactivate the stray price in the Stripe dashboard, then re-run.
    """
    settings = get_settings()
    problems: list[str] = []
    if str(price["currency"]) != CURRENCY:  # type: ignore[index]
        problems.append(f"currency is {price['currency']!r}, expected {CURRENCY!r}")  # type: ignore[index]
    if int(price["unit_amount"]) != settings.payg_price_per_block_cents:  # type: ignore[index]
        problems.append(
            f"unit_amount is {price['unit_amount']}, "  # type: ignore[index]
            f"expected {settings.payg_price_per_block_cents}"
        )
    recurring = price["recurring"] if "recurring" in price else None  # type: ignore[index, operator]
    usage_type = str(recurring["usage_type"]) if recurring and "usage_type" in recurring else ""
    meter_ref = str(recurring["meter"]) if recurring and "meter" in recurring else ""
    if usage_type != "metered":
        problems.append(f"recurring.usage_type is {usage_type!r}, expected 'metered'")
    if meter_ref != meter_id:
        problems.append(f"recurring.meter is {meter_ref!r}, expected {meter_id!r}")
    return problems


def _ensure_price(stripe_mod: object, *, product_id: str, meter_id: str) -> tuple[object, bool]:
    existing = _find_price(stripe_mod, product_id=product_id)
    if existing is not None:
        return existing, False
    settings = get_settings()
    created = stripe_mod.Price.create(  # type: ignore[attr-defined]
        product=product_id,
        currency=CURRENCY,
        unit_amount=settings.payg_price_per_block_cents,
        nickname=f"Luciel PAYG — {settings.payg_block_size}-conversation block",
        recurring={"usage_type": "metered", "interval": "month", "meter": meter_id},
        metadata={"luciel_key": PRICE_KEY},
    )
    return created, True


def provision(stripe_mod: object, *, verify_only: bool) -> int:
    """Create (or verify) meter → product → price. Returns a process exit code."""
    if verify_only:
        meter = _find_meter(stripe_mod)
        if meter is None:
            print(f"{_P} MISSING: Billing Meter {METER_EVENT_NAME!r}")
            return 1
        product = _find_product(stripe_mod)
        if product is None:
            print(f"{_P} MISSING: product metadata.luciel_key={PRODUCT_KEY!r}")
            return 1
        price = _find_price(stripe_mod, product_id=str(product["id"]))  # type: ignore[index]
        if price is None:
            print(f"{_P} MISSING: price metadata.luciel_key={PRICE_KEY!r}")
            return 1
        problems = _check_price(price, meter_id=str(meter["id"]))  # type: ignore[index]
        if problems:
            for p in problems:
                print(f"{_P} MISMATCH: {p}")
            return 1
        print(f"{_P} OK: meter={meter['id']} product={product['id']} price={price['id']}")  # type: ignore[index]
        return 0

    meter, meter_created = _ensure_meter(stripe_mod)
    meter_id = str(meter["id"])  # type: ignore[index]
    print(f"{_P} 1/3 meter  {meter_id} ({'created' if meter_created else 'exists'})")

    product, product_created = _ensure_product(stripe_mod)
    product_id = str(product["id"])  # type: ignore[index]
    print(f"{_P} 2/3 product {product_id} ({'created' if product_created else 'exists'})")

    existing_price = _find_price(stripe_mod, product_id=product_id)
    if existing_price is not None:
        problems = _check_price(existing_price, meter_id=meter_id)
        if problems:
            print(f"{_P} REFUSING to adopt existing price {existing_price['id']}:")  # type: ignore[index]
            for p in problems:
                print(f"{_P}   - {p}")
            print(f"{_P} Deactivate that price in the Stripe dashboard and re-run.")
            return 1
    price, price_created = _ensure_price(stripe_mod, product_id=product_id, meter_id=meter_id)
    price_id = str(price["id"])  # type: ignore[index]
    print(f"{_P} 3/3 price  {price_id} ({'created' if price_created else 'exists'})")

    print(f"{_P}")
    print(f"{_P} Done. Now put the id where the platform reads it:")
    print(f"{_P}   STRIPE_PAYG_PRICE_ID={price_id}")
    print(f"{_P} in the luciel/<env>/platform secret (with STRIPE_TEST_SECRET_KEY /")
    print(f"{_P} STRIPE_TEST_PUBLISHABLE_KEY / STRIPE_TEST_WEBHOOK_SECRET), then restart")
    print(f"{_P} the ECS services. Env alternative: LUCIEL_STRIPE_PAYG_PRICE_ID.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--verify",
        action="store_true",
        help="read-only: exit 0 iff meter+product+price all exist and agree with config",
    )
    args = parser.parse_args(argv)

    secret_key = os.environ.get("STRIPE_TEST_SECRET_KEY", "")
    if not secret_key:
        print(f"{_P} STRIPE_TEST_SECRET_KEY is not set; nothing was touched.")
        return 1
    if not secret_key.startswith("sk_test_"):
        # Same fail-closed posture as RealStripeGateway: this script must be
        # physically unable to run against a live Stripe account.
        print(
            f"{_P} STRIPE_TEST_SECRET_KEY is not a Stripe TEST key (sk_test_...); "
            "refusing to run. Nothing was touched."
        )
        return 1

    import stripe  # noqa: PLC0415 — lazy import, mirrors the gateway (CI-safe)

    stripe.api_key = secret_key
    return provision(stripe, verify_only=args.verify)


if __name__ == "__main__":
    sys.exit(main())
