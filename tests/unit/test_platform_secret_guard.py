"""An unprovisioned platform secret must not slip past the live-mode fail-closed guards.

Wave 2 added two guards to ``Settings.model_post_init``: live mode refuses to boot if
``JWT_SIGNING_KEY`` is still the dev default published in this repo, or if
``STRIPE_WEBHOOK_SECRET`` is unset/still the published test constant. Both sat BELOW an
``if not blob: return`` early exit, and ``platform_secret`` defaults to ``"{}"`` —
which parses to an empty mapping. So the exact configuration the guards exist to catch
(live mode, secret never provisioned) was the one configuration that skipped them: the
service booted live with a forgeable session key and a forgeable billing webhook.

``parse_platform_secret`` also returned ``{}`` for whitespace and for unparsable JSON, so
those inputs took the same shortcut. P2-12 has since split those two cases apart: an
ABSENT secret still parses to an empty blob and falls through to the guards below, but a
MALFORMED one is refused on the spot in live mode — reading it as "not provisioned"
silently disabled every platform credential at once behind a single warning line.
"""

from __future__ import annotations

import json

import pytest
from app.config import PlatformSecretError, Settings

from tests.unit.live_settings import REQUIRED_IN_LIVE_MODE, live_settings

REAL_JWT = "not-the-published-dev-default"  # noqa: S105 — test fixture value
REAL_WEBHOOK = "whsec_provisioned_by_secrets_manager"  # noqa: S105 — test fixture value

# The delivery/HMAC secrets fail closed in live mode too, so a blob that is meant to get
# PAST the guards has to carry them; tests targeting one guard supply the rest.
_GUARDED_ELSEWHERE = ("JWT_SIGNING_KEY", "STRIPE_WEBHOOK_SECRET")
OTHER_REQUIRED = {k: v for k, v in REQUIRED_IN_LIVE_MODE.items() if k not in _GUARDED_ELSEWHERE}

# Every input that parses to an empty blob, i.e. every way of saying "not provisioned".
EMPTY_BLOBS = ["{}", "", "   ", "\n\t "]
# Inputs that are not a readable JSON object. "Cannot be read" is a different condition
# from "was never set", and P2-12 gives it its own, earlier refusal.
MALFORMED_BLOBS = ["not json at all", "[]", "null", '{"truncated": ']


@pytest.mark.parametrize("blob", EMPTY_BLOBS)
def test_live_mode_refuses_to_boot_on_an_empty_platform_secret(blob: str):
    with pytest.raises(RuntimeError, match="JWT_SIGNING_KEY"):
        Settings(integration_mode="live", platform_secret=blob)


@pytest.mark.parametrize("blob", MALFORMED_BLOBS)
def test_live_mode_refuses_to_boot_on_a_malformed_platform_secret(blob: str):
    """P2-12: fail on the blob itself rather than on whichever guard below happens to
    notice a credential missing. The operator needs to be told the secret is corrupt,
    not that one key inside it looks unset."""
    with pytest.raises(PlatformSecretError, match="LUCIEL_PLATFORM_SECRET"):
        Settings(integration_mode="live", platform_secret=blob)


@pytest.mark.parametrize("blob", MALFORMED_BLOBS)
def test_mock_mode_still_tolerates_a_malformed_platform_secret(blob: str):
    """CI and local dev never provision a real blob, so a corrupt one stays a warning
    there — the fail-closed behaviour is live-mode only."""
    assert Settings(integration_mode="mock", platform_secret=blob).integration_mode == "mock"


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
            {"JWT_SIGNING_KEY": REAL_JWT, "STRIPE_WEBHOOK_SECRET": REAL_WEBHOOK, **OTHER_REQUIRED}
        ),
    )
    assert s.jwt_signing_key == REAL_JWT
    assert s.stripe_webhook_secret == REAL_WEBHOOK


@pytest.mark.parametrize(
    ("omitted", "attr"),
    [
        ("TWILIO_AUTH_TOKEN", "twilio_auth_token"),
        ("PLATFORM_EMAIL_SENDER", "platform_email_sender"),
    ],
)
def test_live_mode_refuses_to_boot_without_a_delivery_secret(omitted: str, attr: str):
    """P1-9/P1-10: these default to "" and nothing downstream refuses.

    An empty Twilio HMAC key verified a forged webhook rather than skipping
    verification, and an unset sender identity binds ``NullEmailSender``, so every
    platform email is recorded and dropped. Neither is visible from outside, so the
    refusal has to happen at boot. (META_APP_SECRET is conditional on a provisioned
    Meta app since round 7 WP-7 — tests/unit/test_meta_app_posture.py.)
    """
    blob = {k: v for k, v in REQUIRED_IN_LIVE_MODE.items() if k != omitted}
    with pytest.raises(RuntimeError, match=omitted):
        Settings(integration_mode="live", platform_secret=json.dumps(blob))

    # Same blob in mock mode: CI and local dev must keep booting.
    assert getattr(Settings(platform_secret=json.dumps(blob)), attr) == ""


def test_the_delivery_guard_names_every_missing_secret_at_once():
    """One boot, one complete list — an operator should not fix these one deploy apart."""
    with pytest.raises(RuntimeError) as err:
        Settings(
            integration_mode="live",
            platform_secret=json.dumps(
                {"JWT_SIGNING_KEY": REAL_JWT, "STRIPE_WEBHOOK_SECRET": REAL_WEBHOOK}
            ),
        )
    message = str(err.value)
    assert all(name in message for name in OTHER_REQUIRED)
    # Names only: the guard must never echo a provisioned secret's VALUE.
    assert REQUIRED_IN_LIVE_MODE["JWT_SIGNING_KEY"] not in message


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


def test_an_absent_instagram_app_still_boots_in_live_mode():
    """Not every deployment connects Instagram, and the ones that do not must boot. The
    connection reports ``configured=false`` and the owner sees a disabled tile — the same
    honest-disabled posture Meta had before its app was provisioned."""
    s = live_settings()
    assert s.instagram_app_id == ""
    assert s.instagram_app_secret == ""


def test_a_half_provisioned_instagram_app_refuses_to_boot_in_live_mode():
    """An app id with no secret is the posture that must fail closed: the authorize URL
    builds from the id alone, so the owner is sent to a real Instagram consent screen whose
    code the exchange can then never redeem. Failing at boot names the missing key; failing
    at the callback shows the owner "exchange_failed" and nothing actionable."""
    with pytest.raises(RuntimeError, match="INSTAGRAM_APP_SECRET"):
        live_settings(INSTAGRAM_APP_ID="ig-cid")

    with pytest.raises(RuntimeError, match="INSTAGRAM_APP_SECRET"):
        live_settings(INSTAGRAM_REDIRECT_URI="https://api.example.com/cb")


def test_a_fully_provisioned_instagram_app_boots():
    s = live_settings(
        INSTAGRAM_APP_ID="ig-cid",
        INSTAGRAM_APP_SECRET="ig-csecret",
        INSTAGRAM_REDIRECT_URI="https://api.example.com/cb",
        # The Instagram app's webhook subscribes to the same GET /webhooks/meta
        # handshake, so a provisioned Instagram app needs the verify token too.
        META_VERIFY_TOKEN="unit-test-verify-token",
    )
    assert s.instagram_app_id == "ig-cid"
    assert s.instagram_app_secret == "ig-csecret"  # noqa: S105 — test fixture


def test_an_absent_microsoft_app_still_boots_in_live_mode():
    """The BYO Outlook mailbox before its Entra app exists: honestly unconfigured
    (dark tile, no authorize URL), never a boot failure."""
    s = live_settings()
    assert s.microsoft_client_id == ""
    assert s.microsoft_client_secret == ""


def test_a_half_provisioned_microsoft_app_refuses_to_boot_in_live_mode():
    """Same rule as Instagram: an id or redirect without the secret sends the owner
    through a real Microsoft consent screen to a code the exchange can never redeem."""
    with pytest.raises(RuntimeError, match="MICROSOFT_CLIENT_SECRET"):
        live_settings(MICROSOFT_CLIENT_ID="ms-cid")

    with pytest.raises(RuntimeError, match="MICROSOFT_CLIENT_SECRET"):
        live_settings(MICROSOFT_REDIRECT_URI="https://api.example.com/cb")


def test_a_fully_provisioned_microsoft_app_boots():
    s = live_settings(
        MICROSOFT_CLIENT_ID="ms-cid",
        MICROSOFT_CLIENT_SECRET="ms-csecret",
        MICROSOFT_REDIRECT_URI="https://api.example.com/cb",
    )
    assert s.microsoft_client_id == "ms-cid"
    assert s.microsoft_client_secret == "ms-csecret"  # noqa: S105 — test fixture


def test_llm_model_ids_overlay_from_the_platform_secret():
    """§3.4.3 model-retirement contingency: a retired model version is swapped by
    rotating the platform secret + restarting ECS — no image rebuild. The LLM
    fields carry non-empty defaults, so the generic empty-field overlay never
    reaches them; the explicit bridge must."""
    s = live_settings(
        LLM_ANTHROPIC_PRIMARY_MODEL="claude-sonnet-9-9",
        LLM_ANTHROPIC_FAST_MODEL="claude-haiku-9-9",
        LLM_OPENAI_FALLBACK_MODEL="gpt-9.9",
    )
    assert s.llm_anthropic_primary_model == "claude-sonnet-9-9"
    assert s.llm_anthropic_fast_model == "claude-haiku-9-9"
    assert s.llm_openai_fallback_model == "gpt-9.9"


def test_llm_model_env_var_beats_the_platform_secret(monkeypatch: pytest.MonkeyPatch):
    """Env > blob > default: a deliberate task-definition/local pin must not be
    silently overridden by the blob."""
    monkeypatch.setenv("LUCIEL_LLM_ANTHROPIC_PRIMARY_MODEL", "claude-pinned-by-env")
    s = live_settings(LLM_ANTHROPIC_PRIMARY_MODEL="claude-from-blob")
    assert s.llm_anthropic_primary_model == "claude-pinned-by-env"


def test_llm_model_defaults_hold_when_neither_env_nor_blob_sets_them():
    s = live_settings()
    assert s.llm_anthropic_primary_model == "claude-sonnet-4-6"
    assert s.llm_openai_fallback_model == "gpt-5.4"
