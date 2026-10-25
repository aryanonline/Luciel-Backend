"""Configured OAuth redirect URIs are validated, not read raw (round 7 WP-7).

Every provider redirects the admin's browser back to the URI registered on the
platform's OAuth app, and the only route that serves it is
``GET /api/v1/connections/<oauth client label>/callback``. ``oauth_client_config`` read
the configured URI raw, so one registered at another path sent the owner through a real
consent screen to a 404 — or to a redeem the provider refuses because the
``redirect_uri`` does not match. Now, once every config source has been applied:

  * a URI that does not end with the served callback path — or, when the public API
    origin is known, is not exactly that origin plus the path — is logged at ERROR;
  * the boot does NOT fail (the rest of the platform is fine);
  * the provider is withheld from the connect surface (``authorize_ready`` false, so
    no Connect button and no authorize URL), while refresh of existing grants goes on;
  * ``/healthz`` names ``oauth_redirect:<label>``.
"""

from __future__ import annotations

import json
import logging

import pytest
from app.config import OAUTH_CALLBACK_PATH, OAUTH_REDIRECT_URI_FIELDS, Settings, get_settings
from app.connections import oauth_flow, registry
from app.runtime.integration_status import degraded_bindings

from tests.unit.live_settings import live_settings

GOOD = "https://api.example.test/api/v1/connections/hubspot/callback"
WRONG_PATH = "https://api.example.test/oauth/hubspot"


def test_the_label_table_is_the_registry_s_own() -> None:
    """config.py cannot import the registry, so the field->label table is a copy; this
    is what keeps it one."""
    seen: set[str] = set()
    for provider, spec in registry.PROVIDERS.items():
        if not spec.is_oauth or not spec.redirect_uri_setting:
            continue
        assert OAUTH_REDIRECT_URI_FIELDS[spec.redirect_uri_setting] == (
            registry.oauth_client_label(provider)
        ), provider
        seen.add(spec.redirect_uri_setting)
    assert seen == set(OAUTH_REDIRECT_URI_FIELDS)


def test_the_validated_path_is_the_route_the_app_serves() -> None:
    from tests.unit.test_route_registry import EXPECTED_ROUTES

    assert ("GET", OAUTH_CALLBACK_PATH.format(label="{client}")) in EXPECTED_ROUTES


def test_a_wrong_path_is_logged_and_does_not_crash_the_boot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger="app.config"):
        s = live_settings(
            HUBSPOT_CLIENT_ID="hs-cid",
            HUBSPOT_CLIENT_SECRET="hs-secret",
            HUBSPOT_REDIRECT_URI=WRONG_PATH,
        )
    assert s.hubspot_redirect_uri == WRONG_PATH  # kept as configured, never rewritten
    assert set(s.oauth_redirect_problems()) == {"hubspot"}
    assert "HUBSPOT_REDIRECT_URI" in s.oauth_redirect_problems()["hubspot"]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("oauth_redirect_invalid provider=hubspot" in r.getMessage() for r in errors)


def test_a_well_formed_uri_is_not_a_problem() -> None:
    s = Settings(platform_secret=json.dumps({"HUBSPOT_REDIRECT_URI": GOOD}))
    assert s.oauth_redirect_problems() == {}


def test_with_a_known_origin_the_uri_must_be_an_origin_this_environment_serves() -> None:
    other_host = "https://api.elsewhere.test/api/v1/connections/hubspot/callback"
    s = Settings(
        public_api_base_url="https://api.example.test/",
        platform_secret=json.dumps({"HUBSPOT_REDIRECT_URI": other_host}),
    )
    assert (
        "must be https://api.example.test/api/v1/connections/hubspot/callback"
        in (s.oauth_redirect_problems()["hubspot"])
    )
    ok = Settings(
        public_api_base_url="https://api.example.test",
        platform_secret=json.dumps({"HUBSPOT_REDIRECT_URI": GOOD}),
    )
    assert ok.oauth_redirect_problems() == {}


def test_a_uri_on_any_origin_the_environment_serves_is_fine() -> None:
    """Phase 3 review (critical): dev moved to api-dev while every provider console
    still names the public api, which dev keeps serving. The injected origin, the
    environment's other API names and the secret's own API_BASE_URL are all valid;
    a host the environment does not serve is not."""
    on_public_name = "https://api.example.test/api/v1/connections/hubspot/callback"
    s = Settings(
        public_api_base_url="https://api-dev.example.test",
        api_origins="https://api-dev.example.test,https://api.example.test/",
        platform_secret=json.dumps({"HUBSPOT_REDIRECT_URI": on_public_name}),
    )
    assert s.accepted_api_origins() == (
        "https://api-dev.example.test",
        "https://api.example.test",
    )
    assert s.oauth_redirect_problems() == {}
    # The secret's API_BASE_URL counts while nothing says which origins the
    # environment serves (local, tests, a pre-round-7 task definition).
    via_secret = Settings(
        public_api_base_url="https://api-dev.example.test",
        platform_secret=json.dumps(
            {"API_BASE_URL": "https://api.example.test", "HUBSPOT_REDIRECT_URI": on_public_name}
        ),
    )
    assert via_secret.public_api_base_url == "https://api-dev.example.test"
    assert via_secret.oauth_redirect_problems() == {}
    elsewhere = Settings(
        public_api_base_url="https://api-dev.example.test",
        api_origins="https://api.example.test",
        platform_secret=json.dumps(
            {
                "HUBSPOT_REDIRECT_URI": "https://api.elsewhere.test/api/v1/connections/hubspot/callback"
            }
        ),
    )
    problem = elsewhere.oauth_redirect_problems()["hubspot"]
    assert "https://api-dev.example.test/api/v1/connections/hubspot/callback" in problem
    assert "https://api.example.test/api/v1/connections/hubspot/callback" in problem
    # Once the served set IS injected, a stale API_BASE_URL in the secret no longer
    # keeps a callback valid: after the D6 cutover dev stops serving the apex, and
    # an apex callback would land on prod.
    post_cutover = Settings(
        public_api_base_url="https://api-dev.example.test",
        api_origins="https://api-dev.example.test",
        platform_secret=json.dumps(
            {"API_BASE_URL": "https://api.example.test", "HUBSPOT_REDIRECT_URI": on_public_name}
        ),
    )
    assert post_cutover.accepted_api_origins() == ("https://api-dev.example.test",)
    assert "hubspot" in post_cutover.oauth_redirect_problems()


def test_an_unconfigured_provider_is_not_a_problem() -> None:
    assert Settings(platform_secret="{}").oauth_redirect_problems() == {}


@pytest.fixture
def hubspot(monkeypatch: pytest.MonkeyPatch):
    get_settings.cache_clear()  # type: ignore[attr-defined]
    settings = get_settings()
    monkeypatch.setattr(settings, "hubspot_client_id", "hs-cid")
    monkeypatch.setattr(settings, "hubspot_client_secret", "hs-secret")
    yield settings
    get_settings.cache_clear()  # type: ignore[attr-defined]


def test_a_wrong_path_withholds_the_provider_and_names_it_on_healthz(
    hubspot, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uuid

    monkeypatch.setattr(hubspot, "hubspot_redirect_uri", WRONG_PATH)
    cfg = registry.oauth_client_config("hubspot")
    assert cfg.configured is True  # existing grants keep refreshing
    assert cfg.authorize_ready is False
    assert registry.provider_available("hubspot", "crm") is False
    start = oauth_flow.build_authorize_start(
        provider="hubspot",
        admin_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
    )
    assert start.unconfigured is True  # no consent screen is ever opened on it
    assert "oauth_redirect:hubspot" in degraded_bindings()

    monkeypatch.setattr(hubspot, "hubspot_redirect_uri", GOOD)
    assert registry.provider_available("hubspot", "crm") is True
    assert "oauth_redirect:hubspot" not in degraded_bindings()
