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
