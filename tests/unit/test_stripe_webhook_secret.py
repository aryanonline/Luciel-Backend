"""The Stripe webhook signing secret is platform-owned material, not a source constant.

``whsec_fake_test_only`` is published in this repository, so while it was the gateway's
only verifying secret ANY caller could compute a valid ``Stripe-Signature`` and drive
billing-state changes (payment recovered, dunning exit) on any tenant. The secret is now
bridged out of the platform secret and live mode refuses to boot without a real one —
the same fail-closed shape as JWT_SIGNING_KEY, whose default is published the same way.

The fake stays keyed to the constant for mock/CI, which is what lets the rest of the
billing suite forge correctly-signed events with no secret provisioned.
"""

from __future__ import annotations

import json

import pytest
from app.billing.stripe_gateway import (
    FakeStripeGateway,
    SignatureError,
    build_signature_header,
)
from app.config import FAKE_STRIPE_WEBHOOK_SECRET, Settings

from tests.unit.live_settings import live_settings

EVENT = json.dumps({"id": "evt_1", "type": "invoice.paid", "data": {"object": {}}}).encode()
REAL = "whsec_provisioned_by_secrets_manager"  # noqa: S105 — test fixture value


def test_blob_bridges_the_webhook_secret_onto_settings():
    s = Settings(platform_secret=json.dumps({"STRIPE_WEBHOOK_SECRET": REAL}))
    assert s.stripe_webhook_secret == REAL


def test_test_posture_key_name_is_accepted_as_an_alias():
    s = Settings(platform_secret=json.dumps({"STRIPE_TEST_WEBHOOK_SECRET": REAL}))
    assert s.stripe_webhook_secret == REAL


def test_mock_mode_needs_no_secret_provisioned():
    s = Settings(platform_secret="{}")
    assert s.integration_mode == "mock"
    assert s.stripe_webhook_secret == ""


def test_live_mode_refuses_to_boot_without_a_webhook_secret():
    with pytest.raises(RuntimeError, match="STRIPE_WEBHOOK_SECRET"):
        Settings(
            integration_mode="live",
            platform_secret=json.dumps({"JWT_SIGNING_KEY": "real-signing-key"}),
        )


def test_live_mode_refuses_to_boot_with_the_published_fake_secret():
    with pytest.raises(RuntimeError, match="STRIPE_WEBHOOK_SECRET"):
        Settings(
            integration_mode="live",
            platform_secret=json.dumps(
                {
                    "JWT_SIGNING_KEY": "real-signing-key",
                    "STRIPE_WEBHOOK_SECRET": FAKE_STRIPE_WEBHOOK_SECRET,
                }
            ),
        )


def test_live_mode_boots_with_a_real_webhook_secret():
    s = live_settings(STRIPE_WEBHOOK_SECRET=REAL)
    assert s.stripe_webhook_secret == REAL


def test_a_provisioned_secret_rejects_events_signed_with_the_published_constant():
    gw = FakeStripeGateway(webhook_secret=REAL)
    forged = build_signature_header(FAKE_STRIPE_WEBHOOK_SECRET, EVENT)
    with pytest.raises(SignatureError):
        gw.verify_and_parse(payload=EVENT, signature_header=forged)

    # ...and still accepts one signed with the provisioned secret.
    genuine = build_signature_header(REAL, EVENT)
    assert gw.verify_and_parse(payload=EVENT, signature_header=genuine).event_id == "evt_1"


def test_unconfigured_fake_keeps_the_deterministic_test_secret():
    gw = FakeStripeGateway()
    header = build_signature_header(FAKE_STRIPE_WEBHOOK_SECRET, EVENT)
    assert gw.verify_and_parse(payload=EVENT, signature_header=header).type == "invoice.paid"
