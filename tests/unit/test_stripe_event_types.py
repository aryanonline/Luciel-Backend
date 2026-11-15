"""The billing webhook acts only on event types Stripe actually sends.

Axis8-F02 (2026-06-20) added a ``customer.subscription.renewed`` branch. Stripe has no
such event, so the branch never ran and nothing showed it: a handler for a type Stripe
never sends looks the same as one for a real type that has not fired yet. A renewal
arrives as the renewal invoice's ``invoice.paid`` (``billing_reason``
``subscription_cycle``), which the webhook already handles.

``handle_event`` dispatches through one table, ``webhooks._HANDLERS``, whose keys are
``webhooks.HANDLED_EVENT_TYPES``; these tests check those keys. The list of real types
comes from stripe-python itself: ``WebhookEndpointCreateParams.enabled_events`` names
every type a webhook endpoint can subscribe to on the API release the installed SDK
targets (tests/unit/test_stripe_wire.py holds that release to ``STRIPE_API_VERSION``'s).
"""

from __future__ import annotations

from typing import get_args

from app.billing import webhooks
from stripe.params import WebhookEndpointCreateParams


def _stripe_event_types() -> frozenset[str]:
    """Every event type stripe-python lets a webhook endpoint subscribe to ("*" aside)."""
    (literal,) = get_args(WebhookEndpointCreateParams.__annotations__["enabled_events"])
    return frozenset(get_args(literal)) - {"*"}


def test_the_webhook_handles_only_event_types_stripe_sends() -> None:
    unknown = sorted(webhooks.HANDLED_EVENT_TYPES - _stripe_event_types())
    assert not unknown, (
        f"app/billing/webhooks.py handles {unknown}, which Stripe never sends: those "
        "handlers can never run. Find the real event in Stripe's event types reference."
    )


def test_the_handled_types_are_the_ones_the_endpoint_subscribes_to() -> None:
    """The dispatch table is the list, not a copy kept beside it. And the Stripe Dashboard
    endpoint has to subscribe to every type in it, or that handler never runs either:
    a change here is a change to the endpoint and to the restore runbook's list."""
    assert frozenset(webhooks._HANDLERS) == webhooks.HANDLED_EVENT_TYPES
    assert webhooks.HANDLED_EVENT_TYPES == {
        "invoice.paid",
        "invoice.payment_failed",
        "payment_method.attached",
        "payment_method.detached",
    }, (
        "The handled Stripe event types changed: subscribe the webhook endpoint in the "
        "Stripe Dashboard to the new list and update docs/operations/teardown-and-restore.md."
    )


def test_stripe_sends_no_subscription_renewed_event() -> None:
    """The check above would have caught Axis8-F02. A renewal reaches the webhook as an
    ``invoice.paid``. The ``customer.subscription.updated`` sent with it fires on any
    change to a subscription."""
    stripe_types = _stripe_event_types()
    assert "customer.subscription.renewed" not in stripe_types
    assert {"invoice.paid", "customer.subscription.updated"} <= stripe_types
