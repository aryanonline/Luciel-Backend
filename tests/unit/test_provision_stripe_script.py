"""scripts/provision_stripe_test.py — the PAYG billing objects, provisioned safely.

The metering layer is complete but inert until ``stripe_payg_price_id`` names a
real metered price (``test_no_configured_price_reports_nothing``). This script is
the bridge, and what these tests pin is the part that could bill customers
wrongly or touch the wrong account:

  - the meter is created with EXACTLY the payload bindings
    ``RealStripeGateway.report_meter_event`` posts (a mismatch silently drops
    every reported block);
  - provisioning is idempotent — a second run creates nothing;
  - a tagged price whose currency/amount disagree with config is REFUSED, never
    adopted (a wrong price is an incorrect charge, worse than none);
  - a non-sk_test_ key is refused before the stripe SDK is even imported.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

from app.billing.stripe_gateway import METER_EVENT_NAME
from app.config import get_settings

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "provision_stripe_test.py"
_spec = importlib.util.spec_from_file_location("provision_stripe_test", _SCRIPT)
assert _spec and _spec.loader
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)


def make_fake_stripe() -> tuple[types.SimpleNamespace, dict]:
    """A fake stripe module speaking the exact subset the script uses."""
    state: dict = {"meters": [], "products": [], "prices": [], "creates": 0}

    class Meter:
        @staticmethod
        def list(**_kw):
            return {"data": list(state["meters"])}

        @staticmethod
        def create(**kw):
            state["creates"] += 1
            meter = {"id": f"mtr_{len(state['meters']) + 1}", **kw}
            state["meters"].append(meter)
            return meter

    class Product:
        @staticmethod
        def list(**_kw):
            return {"data": list(state["products"])}

        @staticmethod
        def create(**kw):
            state["creates"] += 1
            product = {"id": f"prod_{len(state['products']) + 1}", **kw}
            state["products"].append(product)
            return product

    class Price:
        @staticmethod
        def list(product=None, **_kw):
            return {"data": [p for p in state["prices"] if p.get("product") == product]}

        @staticmethod
        def create(**kw):
            state["creates"] += 1
            price = {"id": f"price_{len(state['prices']) + 1}", **kw}
            state["prices"].append(price)
            return price

    mod = types.SimpleNamespace(
        billing=types.SimpleNamespace(Meter=Meter), Product=Product, Price=Price, api_key=None
    )
    return mod, state


def test_fresh_account_provisions_meter_product_price_with_the_gateway_shape():
    fake, state = make_fake_stripe()
    assert script.provision(fake, verify_only=False) == 0

    (meter,) = state["meters"]
    # THE contract with RealStripeGateway.report_meter_event: event name and the
    # two payload bindings. Drift here = every reported block silently dropped.
    assert meter["event_name"] == METER_EVENT_NAME
    assert meter["default_aggregation"] == {"formula": "sum"}
    assert meter["value_settings"] == {"event_payload_key": "value"}
    assert meter["customer_mapping"] == {
        "event_payload_key": "stripe_customer_id",
        "type": "by_id",
    }

    (product,) = state["products"]
    assert product["metadata"] == {"luciel_key": "payg_block"}

    (price,) = state["prices"]
    assert price["currency"] == "cad"  # owner-confirmed billing currency
    assert price["unit_amount"] == get_settings().payg_price_per_block_cents
    assert price["recurring"] == {
        "usage_type": "metered",
        "interval": "month",
        "meter": meter["id"],
    }
    assert price["metadata"] == {"luciel_key": "payg_block_3900"}


def test_a_second_run_creates_nothing():
    fake, state = make_fake_stripe()
    assert script.provision(fake, verify_only=False) == 0
    creates_after_first = state["creates"]
    assert script.provision(fake, verify_only=False) == 0
    assert state["creates"] == creates_after_first  # idempotent: found, not recreated


def test_verify_mode_fails_empty_and_passes_after_provisioning():
    fake, _ = make_fake_stripe()
    assert script.provision(fake, verify_only=True) == 1
    assert script.provision(fake, verify_only=False) == 0
    assert script.provision(fake, verify_only=True) == 0


def test_a_mismatched_tagged_price_is_refused_not_adopted():
    fake, state = make_fake_stripe()
    assert script.provision(fake, verify_only=False) == 0
    # Sabotage: same tag, wrong currency — as if someone experimented in USD.
    state["prices"][0]["currency"] = "usd"

    creates_before = state["creates"]
    assert script.provision(fake, verify_only=False) == 1
    assert state["creates"] == creates_before  # refused loudly; nothing new created
    assert script.provision(fake, verify_only=True) == 1


def test_non_test_key_is_refused_before_the_sdk_loads(monkeypatch, capsys):
    class _Boom:
        def __getattr__(self, name):  # any attribute access = the SDK was touched
            raise AssertionError("stripe SDK must not be imported for a refused key")

    monkeypatch.setitem(sys.modules, "stripe", _Boom())
    monkeypatch.setenv("STRIPE_TEST_SECRET_KEY", "sk_live_absolutely_not")
    assert script.main([]) == 1
    assert "refusing to run" in capsys.readouterr().out

    monkeypatch.delenv("STRIPE_TEST_SECRET_KEY")
    assert script.main([]) == 1  # missing key: same safe exit, nothing touched
