"""Twilio one-click OAuth connect (audit round 3, C10 — owner decision 2026-08-23).

The registry's twilio entry becomes OAuth-PRIMARY with the credential form as the
explicit "use API keys instead" fallback. Pins the availability rule (the form
needs no platform OAuth app, so twilio is NEVER "not available yet"), the honest
degrade of the start flow when the OAuth app is unprovisioned, the callback
label, the post-exchange account verifier, and the refresh spec.
"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlparse

import pytest
from app.config import Settings
from app.connections import oauth_flow, registry
from app.connections.refresh import REFRESH_SPECS
from app.connections.verify import (
    POST_EXCHANGE_VERIFIERS,
    PostExchangeVerifyFailed,
    verify_twilio_account,
)


def test_twilio_spec_is_oauth_primary_with_the_form_fallback():
    spec = registry.get_provider("twilio")
    assert spec is not None
    assert spec.auth_kind == registry.AUTH_OAUTH
    assert spec.authorize is not None
    assert spec.credential_fallback is True
    # The fallback form still collects the exact credential-path fields.
    assert {f.name for f in spec.credential_fields} == {
        "accountSid",
        "authToken",
        "apiKeySid",
        "apiKeySecret",
    }
    assert spec.auth_class == "oauth_token"


def test_twilio_callback_label_is_twilio_not_twilio_oauth():
    """The redirect URI the owner registers is .../connections/twilio/callback —
    the OAuth-field namespacing (twilio_oauth_client_id) must not leak into it."""
    assert registry.oauth_client_label("twilio") == "twilio"


def test_twilio_stays_available_with_no_platform_oauth_app(monkeypatch):
    """The customer's own API keys need nothing from the platform, so an
    environment without TWILIO_OAUTH_* must still offer Twilio (the pre-C10
    always-available posture) — never a "Not available yet" row."""
    monkeypatch.setattr(registry, "get_settings", lambda: Settings())
    assert registry.provider_available("twilio") is True
    assert registry.provider_available("twilio", "sms_sender") is True


def test_start_degrades_to_the_credential_form_when_oauth_is_unprovisioned(monkeypatch):
    """No dead-end authorize URL: an unconfigured OAuth app means the start
    result IS the client form, exactly what the pre-C10 flow returned."""
    from app.connections import service
    from app.models.connection import InstanceConnection

    monkeypatch.setattr(registry, "get_settings", lambda: Settings())
    conn = InstanceConnection(
        connection_id=uuid.uuid4(),
        admin_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        connection_type="sms_sender",
        provider="twilio",
        auth_class="oauth_token",
        status="unconfigured",
    )
    result = service._start_result("oauth_token", conn, admin_id=conn.admin_id)
    assert result == {"requires_client_form": True}


def test_start_yields_a_real_twilio_consent_url_when_configured(monkeypatch):
    settings = Settings(
        twilio_oauth_client_id="OQtest",
        twilio_oauth_client_secret="oauth-secret",
        twilio_oauth_redirect_uri="https://api.vantagemind.ai/api/v1/connections/twilio/callback",
    )
    monkeypatch.setattr(registry, "get_settings", lambda: settings)
    start = oauth_flow.build_authorize_start(
        provider="twilio",
        admin_id=uuid.uuid4(),
        instance_id=uuid.uuid4(),
        connection_id=uuid.uuid4(),
    )
    assert not start.unconfigured
    parsed = urlparse(start.authorize_url)
    assert parsed.netloc == "login.twilio.com"
    params = parse_qs(parsed.query)
    assert params["client_id"] == ["OQtest"]
    assert params["redirect_uri"][0].endswith("/connections/twilio/callback")
    assert params["state"][0]


def test_twilio_has_a_refresh_spec():
    spec = REFRESH_SPECS["twilio"]
    assert spec.endpoint == "https://login.twilio.com/oauth2/token"


class _FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _FakeTransport:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, str] | None]] = []

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> _FakeResponse:
        self.calls.append((url, headers))
        return self.response


@pytest.mark.asyncio
async def test_account_verifier_binds_the_account_sid_and_expiry():
    transport = _FakeTransport(
        _FakeResponse(
            200,
            {"accounts": [{"sid": "AC" + "7" * 32, "friendly_name": "Acme Plumbing"}]},
        )
    )
    updates = await verify_twilio_account(
        material={"access_token": "tok", "expires_in": "3600"}, transport=transport
    )
    assert updates["accountSid"] == "AC" + "7" * 32
    assert updates["accountName"] == "Acme Plumbing"
    assert updates["mode"] == "oauth"
    assert "token_expires_at" in updates
    # Bearer auth, against the Accounts list.
    url, headers = transport.calls[0]
    assert url.endswith("/Accounts.json")
    assert headers == {"Authorization": "Bearer tok"}
    # It is registered for the callback's verifier seam.
    assert POST_EXCHANGE_VERIFIERS["twilio"] is verify_twilio_account


@pytest.mark.asyncio
async def test_account_verifier_fails_closed_on_rejection_and_empty_grants():
    with pytest.raises(PostExchangeVerifyFailed):
        await verify_twilio_account(
            material={"access_token": "tok"}, transport=_FakeTransport(_FakeResponse(401, {}))
        )
    with pytest.raises(PostExchangeVerifyFailed):
        await verify_twilio_account(
            material={"access_token": "tok"},
            transport=_FakeTransport(_FakeResponse(200, {"accounts": []})),
        )
    with pytest.raises(PostExchangeVerifyFailed):
        await verify_twilio_account(material={}, transport=_FakeTransport(_FakeResponse(200, {})))
