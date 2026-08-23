"""Twilio one-click OAuth connect over the public API (audit round 3, C10).

Connect → exchange → verify (bearer ``GET /Accounts``) → bind, with §3.1.4
honesty intact: a freshly authorized account with NO designated number lands on
"add your number", never a false Connected. A failed account verify keeps the
row un-bound with an actionable reason. And the credential form remains the
explicit fallback even with the OAuth app configured — stamping the row back to
the api_key behavior class.
"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from app.config import Settings
from app.connections.secrets import get_secret_store
from app.db.session import tenant_session
from app.models.connection import InstanceConnection
from sqlalchemy import select

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

TOKEN_URL = "https://login.twilio.com/oauth2/token"  # noqa: S105
ACCOUNTS_URL = "https://api.twilio.com/2010-04-01/Accounts.json"
ACCOUNT_SID = "AC" + "4" * 32


async def _ready_account_with_luciel(client) -> uuid.UUID:
    from app.auth import access
    from app.auth.tokens import decode_token
    from app.config import get_settings

    email = f"two-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "pw-123456", "captchaToken": "t"}
    )
    _a, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    await client.post(
        "/api/v1/admin/luciel", json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"}
    )
    return uuid.UUID(decode_token(client.cookies.get(get_settings().session_cookie_name))["sub"])


def _configure_twilio_oauth(monkeypatch) -> None:
    from app.connections import registry as registry_module
    from app.knowledge import providers as providers_module

    settings = Settings(
        twilio_oauth_client_id="OQtest",
        twilio_oauth_client_secret="tw-oauth-secret",  # noqa: S106 — test fixture
        twilio_oauth_redirect_uri="https://api.example.com/api/v1/connections/twilio/callback",
    )
    monkeypatch.setattr(registry_module, "get_settings", lambda: settings)
    monkeypatch.setattr(providers_module, "get_settings", lambda: settings)


def _fake_twilio_transport(monkeypatch, *, fail_accounts: bool = False):
    import app.api.v1.admin.connections as connections_api

    class FakeTwilio:
        def __init__(self) -> None:
            self.token_calls: list[dict] = []
            self.accounts_calls: list[dict] = []

        async def post(self, url, *, data=None, headers=None):
            self.token_calls.append(dict(data or {}))
            assert url == TOKEN_URL
            return httpx.Response(
                200,
                json={
                    "access_token": "tw-bearer-1",
                    "refresh_token": "tw-refresh-1",
                    "expires_in": 3600,
                },
                request=httpx.Request("POST", url),
            )

        async def get(self, url, *, headers=None):
            self.accounts_calls.append(dict(headers or {}))
            assert url == ACCOUNTS_URL
            if fail_accounts:
                return httpx.Response(401, json={"code": 20003}, request=httpx.Request("GET", url))
            return httpx.Response(
                200,
                json={"accounts": [{"sid": ACCOUNT_SID, "friendly_name": "Acme Plumbing"}]},
                request=httpx.Request("GET", url),
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    transport = FakeTwilio()
    monkeypatch.setattr(connections_api, "oauth_transport", lambda: transport)
    return transport


async def _start_oauth(client) -> tuple[str, str]:
    """Start the connect; return (connection_id, single-use state)."""
    r = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "sms_sender", "provider": "twilio"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("authorizeUrl"), body  # OAuth-primary when the app is configured
    parsed = urlparse(body["authorizeUrl"])
    assert parsed.netloc == "login.twilio.com"
    rows = (await client.get("/api/v1/admin/connections")).json()
    cid = next(row["connectionId"] for row in rows if row["connectionType"] == "sms_sender")
    return str(cid), parse_qs(parsed.query)["state"][0]


async def _row(admin_id: uuid.UUID) -> InstanceConnection:
    async with tenant_session(admin_id) as s:
        return (
            await s.execute(
                select(InstanceConnection).where(
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.connection_type == "sms_sender",
                )
            )
        ).scalar_one()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_oauth_connect_binds_the_account_but_still_asks_for_a_number(client, monkeypatch):
    admin_id = await _ready_account_with_luciel(client)
    _configure_twilio_oauth(monkeypatch)
    transport = _fake_twilio_transport(monkeypatch)

    cid, state = await _start_oauth(client)
    r = await client.post(
        f"/api/v1/admin/connections/{cid}/oauth-callback",
        json={"code": "auth-code-1", "state": state},
    )
    assert r.status_code == 200, r.text
    # The verifier ran with the bearer token.
    assert transport.accounts_calls == [{"Authorization": "Bearer tw-bearer-1"}]

    row = await _row(admin_id)
    cfg = row.non_secret_config or {}
    assert cfg.get("accountSid") == ACCOUNT_SID
    assert cfg.get("accountName") == "Acme Plumbing"
    assert cfg.get("mode") == "oauth"
    assert cfg.get("token_expires_at")
    # §3.1.4: no designated number -> NOT connected, with the add-your-number ask.
    assert row.status == "unconfigured"
    assert "add your number" in (row.status_detail or "")
    assert row.auth_class == "oauth_token"
    # The grant is in the SecretStore, never on the row.
    material = await get_secret_store().get(row.secret_ref) or {}
    assert material.get("access_token") == "tw-bearer-1"
    assert material.get("refresh_token") == "tw-refresh-1"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_failed_account_verify_keeps_the_row_unbound(client, monkeypatch):
    admin_id = await _ready_account_with_luciel(client)
    _configure_twilio_oauth(monkeypatch)
    _fake_twilio_transport(monkeypatch, fail_accounts=True)

    cid, state = await _start_oauth(client)
    r = await client.post(
        f"/api/v1/admin/connections/{cid}/oauth-callback",
        json={"code": "auth-code-1", "state": state},
    )
    assert r.status_code == 409
    row = await _row(admin_id)
    assert (row.non_secret_config or {}).get("accountSid") is None
    assert row.status != "connected"
    assert "oauth_exchange_failed" in (row.status_detail or "")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_credential_form_fallback_stamps_the_api_key_class(client, monkeypatch):
    """ "Use API keys instead" stays a full path with OAuth configured — and the
    row's behavior class follows the credential actually held, so health/refresh
    never chase a refresh token a form tenant does not have."""
    admin_id = await _ready_account_with_luciel(client)
    _configure_twilio_oauth(monkeypatch)

    cid, _state = await _start_oauth(client)
    r = await client.post(
        f"/api/v1/admin/connections/{cid}/credentials",
        json={"fields": {"accountSid": "AC" + "8" * 32, "authToken": "tok"}},
    )
    assert r.status_code == 200, r.text
    row = await _row(admin_id)
    assert row.auth_class == "api_key"
    assert (row.non_secret_config or {}).get("accountSid") == "AC" + "8" * 32
