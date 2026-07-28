"""An unprovisioned platform secret must not slip past the live-mode fail-closed guards.

Wave 2 added two guards to ``Settings.model_post_init``: live mode refuses to boot if
``JWT_SIGNING_KEY`` is still the dev default published in this repo, or if
``STRIPE_WEBHOOK_SECRET`` is unset/still the published test constant. Both sat BELOW an
``if not blob: return`` early exit, and ``platform_secret`` defaults to ``"{}"`` —
which parses to an empty mapping. So the exact configuration the guards exist to catch
(live mode, secret never provisioned) was the one configuration that skipped them: the
service booted live with a forgeable session key and a forgeable billing webhook.

``parse_platform_secret`` also returns ``{}`` for whitespace and for unparsable JSON, so
those inputs took the same shortcut.
"""

from __future__ import annotations

import json

import pytest
from app.config import Settings

REAL_JWT = "not-the-published-dev-default"  # noqa: S105 — test fixture value
REAL_WEBHOOK = "whsec_provisioned_by_secrets_manager"  # noqa: S105 — test fixture value

# Every input that parses to an empty blob, i.e. every way of saying "not provisioned".
EMPTY_BLOBS = ["{}", "", "   ", "\n\t ", "not json at all", "[]", "null"]


@pytest.mark.parametrize("blob", EMPTY_BLOBS)
def test_live_mode_refuses_to_boot_on_an_empty_platform_secret(blob: str):
    with pytest.raises(RuntimeError, match="JWT_SIGNING_KEY"):
        Settings(integration_mode="live", platform_secret=blob)


def test_live_mode_still_refuses_when_only_the_jwt_key_is_provisioned():
    """The Stripe guard sits after the JWT guard; reaching it proves neither is skipped."""
    with pytest.raises(RuntimeError, match="STRIPE_WEBHOOK_SECRET"):
        Settings(
            integration_mode="live",
            platform_secret=json.dumps({"JWT_SIGNING_KEY": REAL_JWT}),
        )


def test_live_mode_boots_with_a_fully_populated_blob():
    s = Settings(
        integration_mode="live",
        platform_secret=json.dumps(
            {"JWT_SIGNING_KEY": REAL_JWT, "STRIPE_WEBHOOK_SECRET": REAL_WEBHOOK}
        ),
    )
    assert s.jwt_signing_key == REAL_JWT
    assert s.stripe_webhook_secret == REAL_WEBHOOK


@pytest.mark.parametrize("blob", EMPTY_BLOBS)
def test_mock_mode_boots_with_no_secret_provisioned(blob: str):
    """The guards are live-mode-only: CI and local dev must keep booting with no secret,
    which is what the removed early return was (incorrectly) relied on to provide."""
    s = Settings(platform_secret=blob)
    assert s.integration_mode == "mock"
    assert s.stripe_webhook_secret == ""


def test_an_empty_blob_leaves_overlay_fields_untouched():
    """Removing the early return means the overlay code below it now runs on an empty
    mapping. Every branch there is `blob.get(...)`-gated, so nothing may change."""
    empty = Settings(platform_secret="{}")
    default = Settings(platform_secret='{"UNRELATED_KEY": "x"}')
    for field in ("app_base_url", "cookie_domain", "hcaptcha_secret_key", "dashboard_origins"):
        assert getattr(empty, field) == getattr(default, field)
    assert empty.allow_captcha_bypass_token is False
