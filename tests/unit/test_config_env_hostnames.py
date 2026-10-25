"""The environment's own hostnames reach the app settings (round 7 WP-8b, owner decision D6).

Dev moved to dev.vantagemind.ai / api-dev.vantagemind.ai while still serving the
public names as a dual-host. The CDK now injects each environment's origins as task
env (infra/lib/compute-stack.ts: LUCIEL_APP_BASE_URL, LUCIEL_PUBLIC_API_BASE_URL,
LUCIEL_DASHBOARD_ORIGINS, derived from env-config.ts ``hostnames``). Before this the
platform secret's APP_BASE_URL overrode ANY app_base_url — so a secret still holding
the apex would have kept every customer-facing link and the session cookie on the
apex no matter what the environment said — and the cookie's registrable domain was
read from the secret alone.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from app.config import Settings

_COMPUTE_STACK = Path(__file__).resolve().parents[2] / "infra" / "lib" / "compute-stack.ts"
_SECRET_WITH_APEX = json.dumps(
    {"APP_BASE_URL": "https://vantagemind.ai", "API_BASE_URL": "https://api.vantagemind.ai"}
)


def test_the_environment_app_origin_beats_a_secret_still_naming_the_apex(monkeypatch):
    monkeypatch.setenv("LUCIEL_APP_BASE_URL", "https://dev.vantagemind.ai")
    s = Settings(platform_secret=_SECRET_WITH_APEX)
    assert s.app_base_url == "https://dev.vantagemind.ai"


def test_the_environment_api_origin_beats_the_secret_too(monkeypatch):
    # public_api_base_url always had this precedence; pinned beside app_base_url so
    # the two can never drift apart again.
    monkeypatch.setenv("LUCIEL_PUBLIC_API_BASE_URL", "https://api-dev.vantagemind.ai")
    s = Settings(platform_secret=_SECRET_WITH_APEX)
    assert s.public_api_base_url == "https://api-dev.vantagemind.ai"


def test_the_secret_still_fills_an_environment_that_sets_nothing():
    s = Settings(platform_secret=_SECRET_WITH_APEX)
    assert s.app_base_url == "https://vantagemind.ai"
    assert s.public_api_base_url == "https://api.vantagemind.ai"


@pytest.mark.parametrize(
    ("app_origin", "cookie_domain"),
    [
        # dev's own name and the API's sibling host (api-dev.) share .vantagemind.ai.
        ("https://dev.vantagemind.ai", ".vantagemind.ai"),
        # Unchanged for the apex.
        ("https://vantagemind.ai", ".vantagemind.ai"),
        ("https://www.vantagemind.ai", ".vantagemind.ai"),
    ],
)
def test_the_cookie_domain_is_the_app_hostnames_registrable_domain(
    monkeypatch, app_origin: str, cookie_domain: str
):
    monkeypatch.setenv("LUCIEL_APP_BASE_URL", app_origin)
    # No APP_BASE_URL in the secret at all: the derivation used to read ONLY the
    # secret, so an environment-set origin left the cookie host-only to the API.
    assert Settings(platform_secret="{}").cookie_domain == cookie_domain


def test_local_and_explicit_cookie_domains_are_untouched(monkeypatch):
    assert Settings(platform_secret="{}").cookie_domain == ""  # 127.0.0.1 default
    monkeypatch.setenv("LUCIEL_APP_BASE_URL", "http://localhost:3000")
    assert Settings(platform_secret="{}").cookie_domain == ""
    monkeypatch.setenv("LUCIEL_APP_BASE_URL", "https://dev.vantagemind.ai")
    monkeypatch.setenv("LUCIEL_COOKIE_DOMAIN", ".dev.example")
    assert Settings(platform_secret="{}").cookie_domain == ".dev.example"


def test_every_dual_host_app_origin_passes_cors(monkeypatch):
    """The CDK injects every app origin the environment serves (dev: its own name
    plus the apex and www it still serves) as JSON; the dashboard on ANY of them
    must pass the preflight, and the secret's origins stay folded in."""
    monkeypatch.setenv("LUCIEL_APP_BASE_URL", "https://dev.vantagemind.ai")
    monkeypatch.setenv(
        "LUCIEL_DASHBOARD_ORIGINS",
        json.dumps(
            ["https://dev.vantagemind.ai", "https://vantagemind.ai", "https://www.vantagemind.ai"]
        ),
    )
    s = Settings(platform_secret=_SECRET_WITH_APEX)
    for origin in (
        "https://dev.vantagemind.ai",
        "https://vantagemind.ai",
        "https://www.vantagemind.ai",
    ):
        assert origin in s.dashboard_origins, origin


def test_an_environment_app_origin_alone_still_passes_cors(monkeypatch):
    monkeypatch.setenv("LUCIEL_APP_BASE_URL", "https://dev.vantagemind.ai")
    s = Settings(platform_secret="{}")
    assert "https://dev.vantagemind.ai" in s.dashboard_origins


def test_the_cdk_injects_exactly_the_settings_this_module_reads():
    """Cross-language seam: every hostname variable compute-stack.ts injects must be a
    real Settings field (pydantic's LUCIEL_ prefix), or the injection is silently
    ignored and the secret's value wins after all."""
    source = _COMPUTE_STACK.read_text(encoding="utf-8")
    injected = set(
        re.findall(
            r"\b(LUCIEL_(?:APP_BASE_URL|PUBLIC_API_BASE_URL|API_ORIGINS|DASHBOARD_ORIGINS))\s*:",
            source,
        )
    )
    assert injected == {
        "LUCIEL_APP_BASE_URL",
        "LUCIEL_PUBLIC_API_BASE_URL",
        "LUCIEL_API_ORIGINS",
        "LUCIEL_DASHBOARD_ORIGINS",
    }
    for var in injected:
        assert var.removeprefix("LUCIEL_").lower() in Settings.model_fields, var
