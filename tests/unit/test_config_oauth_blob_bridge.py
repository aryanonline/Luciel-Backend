"""The platform-secret blob delivers platform-owned OAuth app credentials (§3.8.3).

Live mode injects LUCIEL_PLATFORM_SECRET (from Secrets Manager); the OAuth *app*
client secrets ride in that blob, same class as the LLM/Stripe keys. A direct env
var still wins (local/CI). Per-tenant tokens are NOT config — they stay secret_refs.
"""

from __future__ import annotations

import json

from app.config import Settings


def test_blob_fills_empty_oauth_fields():
    blob = json.dumps(
        {
            "GOOGLE_DRIVE_CLIENT_ID": "gid",
            "GOOGLE_DRIVE_CLIENT_SECRET": "gsec",
            "GOOGLE_DRIVE_REDIRECT_URI": "https://api.vantagemind.ai/api/v1/connections/google/callback",
            "HUBSPOT_CLIENT_SECRET": "hsec",
            "SALESFORCE_CONSUMER_SECRET": "ssec",
        }
    )
    s = Settings(platform_secret=blob)
    assert s.google_drive_client_id == "gid"
    assert s.google_drive_client_secret == "gsec"
    assert s.google_drive_redirect_uri.endswith("/connections/google/callback")
    assert s.hubspot_client_secret == "hsec"
    assert s.salesforce_consumer_secret == "ssec"


def test_direct_env_var_wins_over_blob(monkeypatch):
    monkeypatch.setenv("LUCIEL_GOOGLE_DRIVE_CLIENT_ID", "env-wins")
    blob = json.dumps({"GOOGLE_DRIVE_CLIENT_ID": "blob-loses"})
    s = Settings(platform_secret=blob)
    assert s.google_drive_client_id == "env-wins"


def test_empty_blob_leaves_fields_empty():
    s = Settings(platform_secret="{}")
    assert s.google_drive_client_secret == ""
    assert s.hubspot_client_secret == ""


def test_per_tenant_token_keys_are_not_bridged():
    # A blob key that is NOT in the platform-owned overlay must never leak onto config.
    blob = json.dumps({"SOME_TENANT_ACCESS_TOKEN": "should-not-appear"})
    s = Settings(platform_secret=blob)
    assert not hasattr(s, "some_tenant_access_token")


def test_blob_app_base_url_overrides_localhost_default():
    """Regression: app_base_url has a non-empty localhost DEFAULT, so the generic
    'fill only if empty' overlay guard would SKIP it — leaving customer-facing links
    (e.g. the verify email link) pointing at 127.0.0.1 in production. It must bridge
    explicitly. (Real bug: emailed verify link opened http://127.0.0.1:3000 and failed
    with ERR_CONNECTION_REFUSED.)"""
    s = Settings(platform_secret=json.dumps({"APP_BASE_URL": "https://vantagemind.ai"}))
    assert s.app_base_url == "https://vantagemind.ai"


def test_app_base_url_falls_back_to_default_when_blob_absent():
    s = Settings(platform_secret="{}")
    assert s.app_base_url == "http://127.0.0.1:3000"


def test_blob_adds_frontend_origin_to_cors():
    """Regression: dashboard_origins (CORS allow-list) defaults to localhost only, so a
    deployed frontend origin must be folded in from the blob — else the browser blocks
    every API call from the real site at the CORS preflight ('click login, nothing
    happens'). app_base_url's origin is always included."""
    s = Settings(platform_secret=json.dumps({"APP_BASE_URL": "https://vantagemind.ai"}))
    assert "https://vantagemind.ai" in s.dashboard_origins
    # localhost defaults preserved for local dev
    assert "http://localhost:3000" in s.dashboard_origins


def test_blob_dashboard_origins_explicit_list():
    s = Settings(platform_secret=json.dumps({"DASHBOARD_ORIGINS": "https://a.com, https://b.com"}))
    assert "https://a.com" in s.dashboard_origins
    assert "https://b.com" in s.dashboard_origins


def test_jwt_signing_key_bridges_from_blob():
    s = Settings(platform_secret=json.dumps({"JWT_SIGNING_KEY": "strong-real-key-123456"}))
    assert s.jwt_signing_key == "strong-real-key-123456"


def test_jwt_dev_default_fails_closed_in_live(monkeypatch):
    """Security: live mode must REFUSE to start on the published dev JWT default
    (else all session tokens are forgeable). Found in the config-bridge audit."""
    monkeypatch.setenv("LUCIEL_INTEGRATION_MODE", "live")
    import pytest

    with pytest.raises(RuntimeError, match="JWT_SIGNING_KEY"):
        Settings(platform_secret=json.dumps({"PLATFORM_EMAIL_SENDER": "info@vantagemind.ai"}))


def test_jwt_dev_default_allowed_in_mock():
    s = Settings(platform_secret="{}")  # mock/local: dev default is fine
    assert s.jwt_signing_key == "dev-only-not-a-secret-rotate-in-prod"
