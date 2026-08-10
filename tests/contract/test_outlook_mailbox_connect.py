"""BYO Outlook mailbox: connect → verify → bind → live, over the public API (§3.1.6a).

The doctrine sentence under test is "it never silently replaces a working sender".
Mechanically that means: a swap from the provisioned SES sender stages `outlook`
alongside it; the OAuth callback exchanges against OUTLOOK's endpoints (the staged
provider, not the row's); Graph ``/me`` must verify and the mailbox must bind as this
tenant's routing destination BEFORE anything cuts over; and on any failure the SES
sender is still serving with an actionable reason on the row.

These paths were dead before this arc: ``swap()`` derived the flow shape from the OLD
row's auth class (a ses→outlook swap answered ``requiresClientForm`` — no consent
screen was ever reachable), the callback resolved the OLD provider (unredeemable),
and nothing production-side ever called ``confirm_reconnect``.
"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from app.config import Settings
from app.connections.secrets import get_secret_store, secret_ref_for
from app.db.session import tenant_session
from app.models.connection import InstanceConnection
from sqlalchemy import select

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"  # noqa: S105
GRAPH_ME = "https://graph.microsoft.com/v1.0/me"
MAILBOX = "owner@fabrikam.example"


async def _ready_account_with_luciel(client) -> uuid.UUID:
    from app.auth import access
    from app.auth.tokens import decode_token
    from app.config import get_settings

    email = f"olk-{uuid.uuid4().hex[:8]}@x.com"
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


def _configure_microsoft(monkeypatch) -> None:
    from app.connections import registry as registry_module
    from app.knowledge import providers as providers_module

    settings = Settings(
        microsoft_client_id="ms-cid",
        microsoft_client_secret="ms-csecret",  # noqa: S106 — test fixture
        microsoft_redirect_uri="https://api.example.com/api/v1/connections/microsoft/callback",
    )
    monkeypatch.setattr(registry_module, "get_settings", lambda: settings)
    monkeypatch.setattr(providers_module, "get_settings", lambda: settings)


def _fake_microsoft_transport(
    monkeypatch,
    *,
    fail_exchange: bool = False,
    fail_me: bool = False,
    mailbox: str = MAILBOX,
):
    """Token endpoint + Graph /me in one fake, matching the oauth_transport seam."""
    import app.api.v1.admin.connections as connections_api

    class FakeMicrosoft:
        def __init__(self) -> None:
            self.token_calls: list[dict] = []
            self.me_calls: list[dict] = []

        async def post(self, url, *, data=None, headers=None):
            self.token_calls.append(dict(data or {}))
            assert url == TOKEN_URL
            if fail_exchange:
                return httpx.Response(
                    400, json={"error": "invalid_grant"}, request=httpx.Request("POST", url)
                )
            return httpx.Response(
                200,
                json={
                    "access_token": "ms-tok-1",
                    "refresh_token": "ms-refresh-1",
                    "expires_in": 3599,
                },
                request=httpx.Request("POST", url),
            )

        async def get(self, url, *, headers=None):
            self.me_calls.append(dict(headers or {}))
            assert url == GRAPH_ME
            if fail_me:
                return httpx.Response(
                    401,
                    json={"error": {"code": "InvalidAuthenticationToken"}},
                    request=httpx.Request("GET", url),
                )
            return httpx.Response(
                200,
                json={"mail": mailbox, "userPrincipalName": "owner_fallback@fabrikam.example"},
                request=httpx.Request("GET", url),
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    transport = FakeMicrosoft()
    monkeypatch.setattr(connections_api, "oauth_transport", lambda: transport)
    return transport


async def _provision_ses(client) -> str:
    """The working sender the mailbox must not silently replace. Returns connection id."""
    r = await client.post("/api/v1/admin/connections/email", json={"mode": "vm_subdomain"})
    assert r.status_code == 200
    rows = (await client.get("/api/v1/admin/connections")).json()
    row = next(c for c in rows if c["connectionType"] == "email_sender")
    assert row["provider"] == "ses"
    return row["connectionId"]


async def _swap_to_outlook(client, connection_id: str) -> str:
    """Start the swap; return the single-use state from the REAL authorize URL."""
    r = await client.post(
        f"/api/v1/admin/connections/{connection_id}/swap", json={"provider": "outlook"}
    )
    assert r.status_code == 200
    body = r.json()
    # The old bug: flow shape came from the ROW's provisioned auth class, so this
    # returned requiresClientForm and no consent screen was ever reachable.
    assert "authorizeUrl" in body and body["authorizeUrl"], body
    parsed = urlparse(body["authorizeUrl"])
    assert parsed.netloc == "login.microsoftonline.com"
    return parse_qs(parsed.query)["state"][0]


async def _row(admin_id: uuid.UUID) -> InstanceConnection:
    async with tenant_session(admin_id) as s:
        return (
            await s.execute(
                select(InstanceConnection).where(
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.connection_type == "email_sender",
                )
            )
        ).scalar_one()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_ses_to_outlook_swap_completes_with_verify_and_bind(client, monkeypatch):
    admin_id = await _ready_account_with_luciel(client)
    _configure_microsoft(monkeypatch)
    transport = _fake_microsoft_transport(monkeypatch)
    connection_id = await _provision_ses(client)

    state = await _swap_to_outlook(client, connection_id)
    # Staged, not cut over: SES is still the live provider mid-swap.
    assert (await _row(admin_id)).provider == "ses"

    r = await client.post(
        f"/api/v1/admin/connections/{connection_id}/oauth-callback",
        json={"code": "auth-code-1", "state": state},
    )
    assert r.status_code == 200, r.text

    row = await _row(admin_id)
    assert row.provider == "outlook"
    assert row.auth_class == "oauth_token"  # flipped WITH the provider (§3.8.5)
    assert row.status == "connected"
    cfg = row.non_secret_config or {}
    assert cfg["address"] == MAILBOX  # Graph /me's answer, not anything client-sent
    assert cfg["destination"] == MAILBOX  # bound for inbound routing
    assert cfg["mode"] == "byo_mailbox"
    assert "token_expires_at" in cfg  # the refresh path's cue (~1h tokens)
    assert "pending_provider" not in cfg

    # The token (with its refresh token) is under the type-keyed secret ref.
    material = await get_secret_store().get(
        secret_ref_for(admin_id, row.instance_id, "email_sender")
    )
    assert material and material["access_token"] == "ms-tok-1"
    assert material["refresh_token"] == "ms-refresh-1"
    # And the exchange really hit Microsoft's endpoint with PKCE.
    assert transport.token_calls and "code_verifier" in transport.token_calls[0]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_failed_graph_verify_leaves_ses_serving(client, monkeypatch):
    """Exchange succeeds, /me rejects the token: the swap must resolve back to the
    live SES sender with an actionable reason — never a half-connected mailbox."""
    admin_id = await _ready_account_with_luciel(client)
    _configure_microsoft(monkeypatch)
    _fake_microsoft_transport(monkeypatch, fail_me=True)
    connection_id = await _provision_ses(client)

    state = await _swap_to_outlook(client, connection_id)
    r = await client.post(
        f"/api/v1/admin/connections/{connection_id}/oauth-callback",
        json={"code": "auth-code-1", "state": state},
    )
    assert r.status_code == 409

    row = await _row(admin_id)
    assert row.provider == "ses"  # the working sender was never replaced
    assert row.status == "connected"  # provisioned SES was connected before the swap
    assert "pending_provider" not in (row.non_secret_config or {})  # staged value discarded
    assert "Graph" in (row.status_detail or "")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_mailbox_held_by_another_tenant_refuses_to_bind(client, monkeypatch):
    """Cross-tenant destination uniqueness: the second account connecting the same
    mailbox must fail closed, or inbound mail would route to whichever row wins."""
    # Tenant A connects the mailbox.
    await _ready_account_with_luciel(client)
    _configure_microsoft(monkeypatch)
    _fake_microsoft_transport(monkeypatch)
    conn_a = await _provision_ses(client)
    state_a = await _swap_to_outlook(client, conn_a)
    r = await client.post(
        f"/api/v1/admin/connections/{conn_a}/oauth-callback",
        json={"code": "code-a", "state": state_a},
    )
    assert r.status_code == 200

    # Tenant B tries the SAME mailbox.
    await client.post("/api/v1/auth/logout")
    admin_b = await _ready_account_with_luciel(client)
    conn_b = await _provision_ses(client)
    state_b = await _swap_to_outlook(client, conn_b)
    r = await client.post(
        f"/api/v1/admin/connections/{conn_b}/oauth-callback",
        json={"code": "code-b", "state": state_b},
    )
    assert r.status_code == 409
    assert "already connected to a different account" in r.json()["message"]

    row_b = await _row(admin_b)
    assert row_b.provider == "ses"  # B's working sender untouched
    assert (row_b.non_secret_config or {}).get("destination") != MAILBOX


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_replayed_state_is_refused_after_a_completed_swap(client, monkeypatch):
    admin_id = await _ready_account_with_luciel(client)
    _configure_microsoft(monkeypatch)
    _fake_microsoft_transport(monkeypatch)
    connection_id = await _provision_ses(client)
    state = await _swap_to_outlook(client, connection_id)

    first = await client.post(
        f"/api/v1/admin/connections/{connection_id}/oauth-callback",
        json={"code": "auth-code-1", "state": state},
    )
    assert first.status_code == 200
    replay = await client.post(
        f"/api/v1/admin/connections/{connection_id}/oauth-callback",
        json={"code": "auth-code-2", "state": state},
    )
    assert replay.status_code == 422  # single-use state: spent is spent (§5.1)
    assert (await _row(admin_id)).provider == "outlook"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_fresh_outlook_connect_without_ses_also_verifies_and_binds(client, monkeypatch):
    """No prior sender to protect: a plain connect still runs verify+bind before
    `connected`, so a dead token can never produce a connected mailbox."""
    admin_id = await _ready_account_with_luciel(client)
    _configure_microsoft(monkeypatch)
    _fake_microsoft_transport(monkeypatch)

    r = await client.post(
        "/api/v1/admin/connections",
        json={"connectionType": "email_sender", "provider": "outlook"},
    )
    assert r.status_code == 200, r.text
    start = r.json()
    assert start["authorizeUrl"]
    state = parse_qs(urlparse(start["authorizeUrl"]).query)["state"][0]
    listed = (await client.get("/api/v1/admin/connections")).json()
    connection_id = next(c["connectionId"] for c in listed if c["connectionType"] == "email_sender")

    r = await client.post(
        f"/api/v1/admin/connections/{connection_id}/oauth-callback",
        json={"code": "auth-code-1", "state": state},
    )
    assert r.status_code == 200, r.text
    row = await _row(admin_id)
    assert row.provider == "outlook"
    assert row.status == "connected"
    assert (row.non_secret_config or {})["destination"] == MAILBOX


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_disconnecting_the_mailbox_releases_its_routing_claim(client, monkeypatch):
    """A dead outlook row must neither keep routing inbound mail nor block the
    mailbox's real owner from connecting it on another account — unlike a BYO phone
    number, the destination was bound BY the platform's verify step, not typed by
    the owner. Reconnect later re-baselines the poll from scratch."""
    from app.channels import routing

    admin_id = await _ready_account_with_luciel(client)
    _configure_microsoft(monkeypatch)
    _fake_microsoft_transport(monkeypatch)
    connection_id = await _provision_ses(client)
    state = await _swap_to_outlook(client, connection_id)
    r = await client.post(
        f"/api/v1/admin/connections/{connection_id}/oauth-callback",
        json={"code": "auth-code-1", "state": state},
    )
    assert r.status_code == 200

    r = await client.post(f"/api/v1/admin/connections/{connection_id}/disconnect")
    assert r.status_code == 200

    row = await _row(admin_id)
    cfg = row.non_secret_config or {}
    for released in ("destination", "address", "mailbox_delta_link", "mailbox_seen_ids"):
        assert released not in cfg, released
    async with tenant_session(admin_id) as s:
        assert await routing.owner_of_destination(s, "email_sender", MAILBOX) is None


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_email_panel_serves_a_connected_mailbox(client, monkeypatch):
    """Found live, on the FIRST real mailbox ever connected: the connect succeeded
    and GET /connections/email 500'd — the OUT schema still only knew the two
    platform modes, so the panel that should have shown the success crashed. The
    read must serve mode byo_mailbox with the mailbox address and no DNS records."""
    await _ready_account_with_luciel(client)
    _configure_microsoft(monkeypatch)
    _fake_microsoft_transport(monkeypatch)
    connection_id = await _provision_ses(client)
    state = await _swap_to_outlook(client, connection_id)
    r = await client.post(
        f"/api/v1/admin/connections/{connection_id}/oauth-callback",
        json={"code": "auth-code-1", "state": state},
    )
    assert r.status_code == 200

    r = await client.get("/api/v1/admin/connections/email")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "byo_mailbox"
    assert body["emailAddress"] == MAILBOX
    assert body["status"] == "connected"
    assert body["dnsRecords"] is None  # nothing to publish for a mailbox


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_byo_mailbox_cannot_be_provisioned_only_connected(client):
    """The OUT enum gained byo_mailbox; the REQUEST deliberately did not. A mailbox
    arrives through connect/verify/bind — a POST asking for it is a caller bug and
    must be rejected, not silently treated as some platform mode."""
    await _ready_account_with_luciel(client)

    r = await client.post("/api/v1/admin/connections/email", json={"mode": "byo_mailbox"})

    assert r.status_code == 422
