"""The billing webhook acts only on event types Stripe actually sends.

Axis8-F02 (2026-06-20) added a ``customer.subscription.renewed`` branch. Stripe has no
such event, so the branch never ran and nothing showed it: a branch for a type Stripe
never sends looks the same as one for a real type that has not fired yet. A renewal
arrives as the renewal invoice's ``invoice.paid`` (``billing_reason``
``subscription_cycle``), which the webhook already handles.

The list of real types comes from stripe-python itself:
``WebhookEndpointCreateParams.enabled_events`` names every type a webhook endpoint can
subscribe to on the API release the installed SDK targets
(tests/unit/test_stripe_wire.py holds that release to ``STRIPE_API_VERSION``'s).
"""

from __future__ import annotations

import inspect
import re
from typing import get_args

from app.billing import webhooks
from stripe.params import WebhookEndpointCreateParams


def _stripe_event_types() -> frozenset[str]:
    """Every event type stripe-python lets a webhook endpoint subscribe to ("*" aside)."""
    (literal,) = get_args(WebhookEndpointCreateParams.__annotations__["enabled_events"])
    return frozenset(get_args(literal)) - {"*"}


def _dispatched_event_types() -> frozenset[str]:
    """Every event type app/billing/webhooks.py compares ``event.type`` against."""
    source = inspect.getsource(webhooks)
    return frozenset(re.findall(r'event\.type\s*[!=]=\s*"([^"]+)"', source))


def test_the_webhook_dispatches_only_on_event_types_stripe_sends() -> None:
    dispatched = _dispatched_event_types()
    # The reset path must be among them. If the dispatch is rewritten in a form this scan
    # cannot read, the test fails here instead of passing with nothing checked.
    assert "invoice.paid" in dispatched
    unknown = sorted(dispatched - _stripe_event_types())
    assert not unknown, (
        f"app/billing/webhooks.py handles {unknown}, which Stripe never sends: those "
        "branches can never run. Find the real event in Stripe's event types reference."
    )


def test_stripe_sends_no_subscription_renewed_event() -> None:
    """The check above would have caught Axis8-F02. A renewal reaches the webhook as an
    ``invoice.paid``. The ``customer.subscription.updated`` sent with it fires on any
    change to a subscription."""
    stripe_types = _stripe_event_types()
    assert "customer.subscription.renewed" not in stripe_types
    assert {"invoice.paid", "customer.subscription.updated"} <= stripe_types
