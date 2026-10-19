"""GET /api/v1/public/config — the frontend's runtime public configuration (WP-7).

The backend held an hCaptcha SITE key nothing read (the frontend baked a literal at
build time), and the frontend baked its API origin and the widget embed URL the same
way, so a bundle could not follow the environment serving it. This route serves them
from the backend's own settings. The contract:

  * exactly four camelCase keys — the frontend parses this shape and nothing more;
  * PUBLIC values only: no secret setting's value can appear in the body;
  * unauthenticated (signup/contact render the captcha before any session exists);
  * cacheable (``Cache-Control: public, max-age=...``);
  * ``null`` for anything not configured here, never a placeholder.
"""

from __future__ import annotations

import pytest
from app.config import get_settings

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

ROUTE = "/api/v1/public/config"
KEYS = {"hcaptchaSiteKey", "appBaseUrl", "apiBaseUrl", "widgetEmbedUrl"}


async def test_the_body_is_exactly_the_four_public_values(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "hcaptcha_site_key", "10000000-ffff-ffff-ffff-000000000001")
    monkeypatch.setattr(settings, "app_base_url", "https://dev.vantagemind.example/")
    monkeypatch.setattr(settings, "public_api_base_url", "https://api-dev.vantagemind.example/")
    monkeypatch.setattr(
        settings, "widget_embed_url", "https://embed-dev.vantagemind.example/v1/luciel.js"
    )

    r = await client.get(ROUTE)  # no cookie, no header: unauthenticated by design

    assert r.status_code == 200
    body = r.json()
    assert set(body) == KEYS
    assert body == {
        "hcaptchaSiteKey": "10000000-ffff-ffff-ffff-000000000001",
        "appBaseUrl": "https://dev.vantagemind.example",
        "apiBaseUrl": "https://api-dev.vantagemind.example",
        "widgetEmbedUrl": "https://embed-dev.vantagemind.example/v1/luciel.js",
    }
    cache = r.headers.get("cache-control", "")
    assert "public" in cache and "max-age=" in cache


async def test_unconfigured_values_are_null_not_placeholders(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "hcaptcha_site_key", "")
    monkeypatch.setattr(settings, "public_api_base_url", "")
    monkeypatch.setattr(settings, "widget_embed_url", "")

    body = (await client.get(ROUTE)).json()

    assert body["hcaptchaSiteKey"] is None
    assert body["apiBaseUrl"] is None
    assert body["widgetEmbedUrl"] is None
    assert body["appBaseUrl"]  # always configured (localhost default in dev)


async def test_no_secret_value_can_reach_the_body(client, monkeypatch):
    """Every secret-bearing setting gets a recognisable value; none may appear."""
    settings = get_settings()
    secret_fields = [
        name
        for name in type(settings).model_fields
        if any(word in name for word in ("secret", "token", "password", "signing_key"))
        or name in {"platform_secret", "database_url", "database_url_sync", "redis_url"}
    ]
    assert "hcaptcha_secret_key" in secret_fields  # the one that sits beside the site key
    for name in secret_fields:
        if isinstance(getattr(settings, name), str):
            monkeypatch.setattr(settings, name, f"SECRET-SENTINEL-{name}")
    monkeypatch.setattr(settings, "hcaptcha_site_key", "public-site-key")

    raw = (await client.get(ROUTE)).text

    assert "SECRET-SENTINEL" not in raw
    assert "public-site-key" in raw
