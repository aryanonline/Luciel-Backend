"""Build a live-mode ``Settings`` for provider-binding tests.

Live mode fails closed on the platform-wide secrets whose defaults are published in this
repo — the JWT signing key and the Stripe webhook signing secret — so a live-mode
platform-secret blob must carry them or ``Settings`` refuses to construct. Those guards are
not what the provider tests are exercising, so this supplies them and lets each test pass
only the provider keys it actually cares about.

An EMPTY blob is no exception: it used to skip both guards, which is the whole reason a
live deploy could boot with a forgeable session key. Tests that reach live mode through
the environment rather than by constructing ``Settings`` need ``LIVE_PLATFORM_SECRET``.
"""

from __future__ import annotations

import json

from app.config import Settings

REQUIRED_IN_LIVE_MODE = {
    "JWT_SIGNING_KEY": "unit-test-signing-key",
    "STRIPE_WEBHOOK_SECRET": "whsec_unit_test_not_the_published_constant",
    # The inbound-webhook HMAC keys and the platform sender identity fail closed too:
    # an empty HMAC key verifies forged webhooks, and an unset sender identity drops
    # every platform email silently.
    "TWILIO_AUTH_TOKEN": "unit-test-twilio-auth-token",
    "META_APP_SECRET": "unit-test-meta-app-secret",
    "PLATFORM_EMAIL_SENDER": "no-reply@unit.test",
}

LIVE_PLATFORM_SECRET = json.dumps(REQUIRED_IN_LIVE_MODE)


def live_settings(**blob: str) -> Settings:
    return Settings(
        integration_mode="live",
        platform_secret=json.dumps({**REQUIRED_IN_LIVE_MODE, **blob}),
    )
