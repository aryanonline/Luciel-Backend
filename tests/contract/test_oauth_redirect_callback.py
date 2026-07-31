"""GET /api/v1/connections/{client}/callback — the provider's own redirect target (§3.8).

The registered redirect URIs point at this path, and the PROVIDER navigates the admin's
browser here by GET. Until this route existed every live consent screen ended on a 404:
connect-start could mint a real authorize URL, the admin could grant consent, and the
connection still never left ``unconfigured``.

Every case below asserts the same two things — the connection reaches the right state,
and the browser is handed a 3xx back to the dashboard. A 4xx/5xx here would strand the
admin on a blank page at the API host with no way back, so "never an error page" is
itself part of the contract.
"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

CALLBACK = "/api/v1/connections/google/callback"


async def _ready_account_with_luciel(client) -> None:
    from app.auth import access

    email = f"cb-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "pw-123456", "captchaToken": "t"}
    )
    _a, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    await client.post(
        "/api/v1/admin/luciel", json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"}
    )


def _admin_id(client) -> uuid.UUID:
    from app.auth.tokens import decode_token
    from app.config import get_settings

    return uuid.UUID(decode_token(client.cookies.get(get_settings().session_cookie_name))["sub"])


async def _instance_id(admin_id: uuid.UUID) -> uuid.UUID:
    from app.db.session import tenant_session
    from app.models.instance import Instance
    from sqlalchemy import select

    async with tenant_session(admin_id) as s:
        return (
            await s.execute(select(Instance.id).where(Instance.admin_id == admin_id))
        ).scalar_one()


def _configure_google(monkeypatch) -> None:
    """A configured Google OAuth app, seen by BOTH the registry and the exchange.

    Drive and Calendar share this one client (§3.8), which is exactly why the callback
    path segment is ``google`` and not the provider id."""
    from app.config import Settings
    from app.connections import registry as registry_module
    from app.knowledge import providers as providers_module

    settings = Settings(
        google_client_id="g-cid",
        google_client_secret="g-csecret",  # noqa: S106 — test fixture, not a real secret
        google_redirect_uri="https://api.example.com/api/v1/connections/google/callback",
    )
    monkeypatch.setattr(registry_module, "get_settings", lambda: settings)
    monkeypatch.setattr(providers_module, "get_settings", lambda: settings)


def _fake_google_transport(monkeypatch, *, fail: bool = False):
    import app.api.v1.admin.connections as connections_api

    class FakeTokenTransport:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def post(self, url, *, data=None, headers=None):
            self.calls.append(dict(data or {}))
            assert url == "https://oauth2.googleapis.com/token"
            if fail:
                return httpx.Response(
                    400,
                    json={"error": "invalid_grant"},
                    request=httpx.Request("POST", url),
                )
            return httpx.Response(
                200,
                json={"access_token": "g-tok-1", "refresh_token": "g-refresh-1"},
                request=httpx.Request("POST", url),
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    transport = FakeTokenTransport()
    monkeypatch.setattr(connections_api, "oauth_transport", lambda: transport)
    return transport


async def _start_calendar_connect(client) -> tuple[str, str]:
    """Begin a Google Calendar connect; return (state, connection_id)."""
    rs = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "calendar", "provider": "google_calendar"},
    )
    assert rs.status_code == 200, rs.text
    authorize_url = rs.json()["authorizeUrl"]
    assert authorize_url.startswith("https://accounts.google.com/"), authorize_url
    state = parse_qs(urlparse(authorize_url).query)["state"][0]

    listed = (await client.get("/api/v1/admin/connections")).json()
    connection_id = next(c["connectionId"] for c in listed if c["connectionType"] == "calendar")
    return state, connection_id


RETURN_URL = "http://127.0.0.1:3000/dashboard/configure"


def _redirect_params(response) -> dict[str, str]:
    assert response.status_code in (302, 303), f"{response.status_code}: {response.text[:200]}"
    location = response.headers["location"]
    # Always back to the FRONTEND origin, never left on the API host.
    assert location.startswith(f"{RETURN_URL}?"), location
    return {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}


async def _status_of(client, connection_id: str) -> str:
    listed = (await client.get("/api/v1/admin/connections")).json()
    return next(c["status"] for c in listed if c["connectionId"] == connection_id)


# --- the happy path: a live handshake actually completes -------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_valid_state_exchanges_the_code_connects_and_redirects(client, monkeypatch):
    from app.connections.secrets import get_secret_store, secret_ref_for

    _configure_google(monkeypatch)
    transport = _fake_google_transport(monkeypatch)

    await _ready_account_with_luciel(client)
    admin_id = _admin_id(client)
    instance_id = await _instance_id(admin_id)
    state, connection_id = await _start_calendar_connect(client)
    assert await _status_of(client, connection_id) == "unconfigured"

    r = await client.get(CALLBACK, params={"code": "g-code-1", "state": state})

    params = _redirect_params(r)
    assert params["status"] == "connected"
    assert params["provider"] == "google_calendar"
    assert params["connectionId"] == connection_id
    assert await _status_of(client, connection_id) == "connected"

    stored = await get_secret_store().get(secret_ref_for(admin_id, instance_id, "calendar"))
    assert stored is not None, "the callback must persist the token for the calendar"
    assert stored["access_token"] == "g-tok-1"
    assert len(transport.calls) == 1
    # google_calendar is a PKCE provider; the verifier is re-derived from the jti.
    assert transport.calls[0]["code_verifier"]
    # Secret material must never ride the wire, not even in a redirect URL (§3.8.3).
    assert "g-tok-1" not in r.headers["location"]


# --- every failure is a redirect, never an error page ----------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_provider_error_param_redirects_without_touching_the_exchange(client, monkeypatch):
    """The admin clicked "Deny": the provider sends ``?error`` INSTEAD of a code."""
    _configure_google(monkeypatch)
    transport = _fake_google_transport(monkeypatch)

    await _ready_account_with_luciel(client)
    _state, connection_id = await _start_calendar_connect(client)

    r = await client.get(CALLBACK, params={"error": "access_denied", "state": "anything"})

    assert _redirect_params(r) == {
        "status": "error",
        "reason": "provider_denied",
        "client": "google",
    }
    assert transport.calls == []
    assert await _status_of(client, connection_id) == "unconfigured"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_forged_state_is_an_error_redirect_never_a_500(client, monkeypatch):
    _configure_google(monkeypatch)
    transport = _fake_google_transport(monkeypatch)
    await _ready_account_with_luciel(client)

    r = await client.get(CALLBACK, params={"code": "g-code-1", "state": "not-a-jwt"})

    assert _redirect_params(r)["reason"] == "invalid_state"
    assert transport.calls == [], "a forged state must never reach the token endpoint"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_missing_state_is_an_error_redirect(client, monkeypatch):
    _configure_google(monkeypatch)
    _fake_google_transport(monkeypatch)
    await _ready_account_with_luciel(client)

    r = await client.get(CALLBACK, params={"code": "g-code-1"})
    assert _redirect_params(r)["reason"] == "invalid_state"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_replayed_state_is_refused_the_second_time(client, monkeypatch):
    """Single-use (§5.1). Without this, a site that can drive the admin's browser here
    could graft its own Google account onto the tenant's calendar."""
    _configure_google(monkeypatch)
    _fake_google_transport(monkeypatch)

    await _ready_account_with_luciel(client)
    state, connection_id = await _start_calendar_connect(client)

    first = await client.get(CALLBACK, params={"code": "g-code-1", "state": state})
    assert _redirect_params(first)["status"] == "connected"

    replay = await client.get(CALLBACK, params={"code": "g-code-1", "state": state})
    params = _redirect_params(replay)
    assert params["status"] == "error"
    assert params["reason"] == "invalid_state"
    assert params["connectionId"] == connection_id


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_state_for_another_oauth_app_is_refused(client, monkeypatch):
    """A state minted for Google arriving on Salesforce's redirect path is incoherent
    however well it is signed, so it never reaches the exchange."""
    _configure_google(monkeypatch)
    transport = _fake_google_transport(monkeypatch)

    await _ready_account_with_luciel(client)
    state, _cid = await _start_calendar_connect(client)

    r = await client.get(
        "/api/v1/connections/salesforce/callback", params={"code": "c", "state": state}
    )
    assert _redirect_params(r)["reason"] == "provider_mismatch"
    assert transport.calls == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_valid_state_with_no_code_is_an_error_redirect(client, monkeypatch):
    _configure_google(monkeypatch)
    _fake_google_transport(monkeypatch)

    await _ready_account_with_luciel(client)
    state, connection_id = await _start_calendar_connect(client)

    r = await client.get(CALLBACK, params={"state": state})
    params = _redirect_params(r)
    assert params["reason"] == "missing_code"
    assert params["connectionId"] == connection_id


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_rejected_code_redirects_and_records_the_reason(client, monkeypatch):
    """The provider refusing the code leaves the row NOT connected, with the diagnostic
    persisted on it — and the admin back on the dashboard rather than on a 409 body."""
    from app.db.session import tenant_session
    from app.models.connection import InstanceConnection
    from sqlalchemy import select

    _configure_google(monkeypatch)
    _fake_google_transport(monkeypatch, fail=True)

    await _ready_account_with_luciel(client)
    admin_id = _admin_id(client)
    state, connection_id = await _start_calendar_connect(client)

    r = await client.get(CALLBACK, params={"code": "expired", "state": state})

    params = _redirect_params(r)
    assert params["reason"] == "exchange_failed"
    assert params["connectionId"] == connection_id
    assert await _status_of(client, connection_id) != "connected"
    # The provider's wording stays out of the URL (browser history) but is on the row.
    assert "invalid_grant" not in r.headers["location"]

    async with tenant_session(admin_id) as s:
        detail = (
            await s.execute(
                select(InstanceConnection.status_detail).where(
                    InstanceConnection.connection_id == uuid.UUID(connection_id)
                )
            )
        ).scalar_one()
    assert detail, "the exchange failure must outlive the request that discovered it"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_callback_needs_no_admin_session(client, monkeypatch):
    """The provider's navigation is not guaranteed to carry our cookie, so the signed
    single-use state — not the session — is what authorizes this route."""
    _configure_google(monkeypatch)
    _fake_google_transport(monkeypatch)

    await _ready_account_with_luciel(client)
    state, connection_id = await _start_calendar_connect(client)
    client.cookies.clear()

    r = await client.get(CALLBACK, params={"code": "g-code-1", "state": state})
    assert _redirect_params(r)["status"] == "connected"
    assert _redirect_params(r)["connectionId"] == connection_id


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_both_outcomes_land_on_the_configure_page(client, monkeypatch):
    """The return path must name a route the frontend actually serves.

    It pointed at ``/dashboard/connections``, which no longer exists: the admin granted
    consent, the connection stored fine, and the 303 dropped them on a 404. Connections
    live on the Configure page, so both outcomes are pinned to it here — a redirect to a
    dead route is invisible to every other assertion in this file.
    """
    _configure_google(monkeypatch)
    _fake_google_transport(monkeypatch)

    await _ready_account_with_luciel(client)
    state, _cid = await _start_calendar_connect(client)

    ok = await client.get(CALLBACK, params={"code": "g-code-1", "state": state})
    denied = await client.get(CALLBACK, params={"error": "access_denied", "state": "anything"})

    for r in (ok, denied):
        assert urlparse(r.headers["location"]).path == "/dashboard/configure"
    # The outcome still rides along; the path change must not cost the query params.
    assert _redirect_params(ok)["status"] == "connected"
    assert _redirect_params(denied)["status"] == "error"
