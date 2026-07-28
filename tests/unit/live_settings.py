"""Build a live-mode ``Settings`` for provider-binding tests.

Live mode fails closed on the platform-wide secrets whose defaults are published in this
repo — the JWT signing key and the Stripe webhook signing secret — so any non-empty
platform-secret blob must carry them or ``Settings`` refuses to construct. Those guards are
not what the provider tests are exercising, so this supplies them and lets each test pass
only the provider keys it actually cares about.
"""

from __future__ import annotations

import json

from app.config import Settings

REQUIRED_IN_LIVE_MODE = {
    "JWT_SIGNING_KEY": "unit-test-signing-key",
    "STRIPE_WEBHOOK_SECRET": "whsec_unit_test_not_the_published_constant",
}


def live_settings(**blob: str) -> Settings:
    return Settings(
        integration_mode="live",
        platform_secret=json.dumps({**REQUIRED_IN_LIVE_MODE, **blob}),
    )
