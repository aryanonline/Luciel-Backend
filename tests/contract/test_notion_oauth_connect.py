"""Notion tenant-connect completes end-to-end (round 4, GAP-N2/GAP-N4).

The Notion platform OAuth app was provisioned by the owner on 2026-08-24 and the
wiring review found the flow could not complete: Notion's ``/v1/oauth/token``
requires HTTP Basic client auth with a JSON body (the generic form-encoded
``client_secret_post`` gets a 401), and the admin-authed knowledge callback
hard-coded a provider tuple that rejected notion while the browser callback
accepted it. These cases pin the whole handshake on BOTH completion surfaces,
against the same fake-transport seam every other exchange test uses — the fake
asserts the Basic header and JSON body Notion actually demands.
"""

from __future__ import annotations

import base64
import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

pytestmark = [pytest.mark.contract]

CALLBACK = "/api/v1/connections/notion/callback"


async def _ready_account_with_luciel(client) -> None:
    from app.auth import access

    email = f"no-{uuid.uuid4().hex[:8]}@x.com"
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


def _configure_notion(monkeypatch) -> None:
    from app.config import Settings
    from app.connections import registry as registry_module
    from app.knowledge import providers as providers_module

    settings = Settings(
        notion_client_id="n-cid",
        notion_client_secret="n-csecret",  # noqa: S106 — test fixture, not a real secret
        notion_redirect_uri="https://api.example.com/api/v1/connections/notion/callback",
    )
    monkeypatch.setattr(registry_module, "get_settings", lambda: settings)
    monkeypatch.setattr(providers_module, "get_settings", lambda: settings)


def _fake_notion_transport(monkeypatch, *, patch_knowledge: bool = False):
    """Fake token endpoint asserting Notion's REAL contract: Basic client auth,
    JSON body, no credentials in the body."""
    import app.api.v1.admin.connections as connections_api
    import app.api.v1.admin.knowledge as knowledge_api

    class FakeNotionTokenTransport:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def post(self, url, *, data=None, headers=None, json=None):
            assert url == "https://api.notion.com/v1/oauth/token"
            # Notion 401s form-encoded credential bodies — the exchange must send
            # Basic auth + JSON (this fake IS the contract).
            expected = "Basic " + base64.b64encode(b"n-cid:n-csecret").decode()
            assert (headers or {}).get("Authorization") == expected
            assert data is None, "Notion takes a JSON body, not form encoding"
            assert json is not None
            assert "client_id" not in json and "client_secret" not in json
            self.calls.append(dict(json))
            return httpx.Response(
                200,
                json={
                    "access_token": "n-tok-1",
                    "workspace_id": "ws-1",
                    "bot_id": "bot-1",
                },
                request=httpx.Request("POST", url),
            )

        async def get(self, url, *, headers=None, params=None):
            # Round 6 WP-C: the verifier answers as the integration bot (/v1/users/me)
            # and names the workspace before the row flips.
            assert url == "https://api.notion.com/v1/users/me"
            assert (headers or {}).get("Authorization") == "Bearer n-tok-1"
            assert (headers or {}).get("Notion-Version")
            return httpx.Response(
                200,
                json={"type": "bot", "name": "Luciel", "bot": {"workspace_name": "Acme Co"}},
                request=httpx.Request("GET", url),
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    transport = FakeNotionTokenTransport()
    monkeypatch.setattr(connections_api, "oauth_transport", lambda: transport)
    if patch_knowledge:
        monkeypatch.setattr(knowledge_api, "oauth_transport", lambda: transport)
    return transport


async def _start_notion_connect(client) -> tuple[str, str]:
    """Begin the knowledge-sync Notion connect; return (state, connection_id)."""
    rs = await client.post("/api/v1/admin/knowledge/sync-connections", json={"provider": "notion"})
    assert rs.status_code == 201, rs.text
    row = rs.json()
    authorize_url = (row.get("nonSecretConfig") or {}).get("authorize_url")
    assert authorize_url and authorize_url.startswith(
        "https://api.notion.com/v1/oauth/authorize"
    ), authorize_url
    state = parse_qs(urlparse(authorize_url).query)["state"][0]
    return state, row["connectionId"]


async def _status_of(client, connection_id: str) -> str:
    listed = (await client.get("/api/v1/admin/knowledge/sync-connections")).json()
    return next(c["status"] for c in listed if c["connectionId"] == connection_id)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_browser_callback_completes_notion_with_basic_auth_json_exchange(client, monkeypatch):
    from app.connections.secrets import get_secret_store

    _configure_notion(monkeypatch)
    transport = _fake_notion_transport(monkeypatch)

    await _ready_account_with_luciel(client)
    admin_id = _admin_id(client)
    instance_id = await _instance_id(admin_id)
    state, connection_id = await _start_notion_connect(client)
    assert await _status_of(client, connection_id) == "unconfigured"

    r = await client.get(CALLBACK, params={"code": "n-code-1", "state": state})

    assert r.status_code in (302, 303), f"{r.status_code}: {r.text[:200]}"
    params = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
    assert params["status"] == "connected"
    assert params["provider"] == "notion"
    assert await _status_of(client, connection_id) == "connected"

    from app.db.session import tenant_session
    from app.models.connection import InstanceConnection
    from sqlalchemy import select

    # Per-connection secret refs (F092): read the row's own ref, not the legacy one.
    async with tenant_session(admin_id) as s:
        secret_ref = (
            await s.execute(
                select(InstanceConnection.secret_ref).where(
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.connection_id == uuid.UUID(str(connection_id)),
                )
            )
        ).scalar_one()
    assert secret_ref and secret_ref.startswith(f"vm/{admin_id}/{instance_id}/")
    stored = await get_secret_store().get(secret_ref)
    assert stored is not None, "the callback must persist the tenant's workspace token"
    assert stored["access_token"] == "n-tok-1"
    assert len(transport.calls) == 1
    assert transport.calls[0]["code"] == "n-code-1"
    # Secret material must never ride the redirect URL (§3.8.3).
    assert "n-tok-1" not in r.headers["location"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_admin_knowledge_callback_accepts_notion_via_the_registry_gate(client, monkeypatch):
    """GAP-N4: the POST surface used to hard-code ("google_drive","hubspot",
    "salesforce") and 422 notion — the two completion surfaces must agree, so the
    gate now derives from the registry (any registered OAuth provider passes)."""
    _configure_notion(monkeypatch)
    transport = _fake_notion_transport(monkeypatch, patch_knowledge=True)

    await _ready_account_with_luciel(client)
    state, connection_id = await _start_notion_connect(client)

    r = await client.post(
        f"/api/v1/admin/knowledge/sync-connections/{connection_id}/oauth-callback",
        json={"code": "n-code-2", "state": state},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "connected"
    assert len(transport.calls) == 1
    assert await _status_of(client, connection_id) == "connected"
