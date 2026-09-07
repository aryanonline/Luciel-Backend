"""Contract tests for the KNOWLEDGE additions in this build (Architecture §3.2.2 /
§3.2.3): CSV import path, rename + replace (raw knowledge view edit affordances),
and the live-sync connector admin API (create / list / trigger).
"""

from __future__ import annotations

import io
import uuid

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


def _state_from(conn_body: dict) -> str:
    """The signed ``state`` the connect-start minted, read off the authorizeUrl (§5.1).

    The callback only redeems a code carrying THIS attempt's state, so a test that
    completes the flow has to go through the authorize URL like the admin's browser."""
    from urllib.parse import parse_qs, urlparse

    url = (conn_body["nonSecretConfig"] or {})["authorize_url"]
    return parse_qs(urlparse(url).query)["state"][0]


async def _ready_account_with_luciel(client) -> None:
    from app.auth import access

    email = f"kb-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "pw-123456", "captchaToken": "t"}
    )
    _a, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    await client.post(
        "/api/v1/admin/luciel", json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"}
    )


# --- CSV import path --------------------------------------------------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_csv_import_creates_structured_source(client):
    await _ready_account_with_luciel(client)
    csv_bytes = b"name,price\nStarter,899\nGrowth,1499\n"
    r = await client.post(
        "/api/v1/admin/knowledge/sources/csv",
        files={"file": ("pricing.csv", io.BytesIO(csv_bytes), "text/csv")},
        data={"name": "Pricing table"},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["origin"] == "csv"
    assert body["ingestionStatus"] == "ready"

    rc = await client.get(f"/api/v1/admin/knowledge/sources/{body['sourceId']}/chunks")
    assert rc.status_code == 200
    joined = " ".join(c["text"] for c in rc.json())
    assert "Starter" in joined and "899" in joined


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_csv_import_rejects_empty_csv(client):
    await _ready_account_with_luciel(client)
    r = await client.post(
        "/api/v1/admin/knowledge/sources/csv",
        files={"file": ("empty.csv", io.BytesIO(b"name,price\n"), "text/csv")},
        data={"name": "Empty"},
    )
    assert r.status_code == 422


# --- upload endpoint routes PDFs/DOCX/TXT through the real parser ----------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_upload_txt_still_works_like_before(client):
    await _ready_account_with_luciel(client)
    r = await client.post(
        "/api/v1/admin/knowledge/sources/upload",
        files={
            "file": ("notes.txt", io.BytesIO(b"Our hours are nine to five weekdays."), "text/plain")
        },
        data={"name": "Hours"},
    )
    assert r.status_code == 201
    assert r.json()["origin"] == "upload"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_upload_rejects_unparseable_pdf_with_422(client):
    await _ready_account_with_luciel(client)
    r = await client.post(
        "/api/v1/admin/knowledge/sources/upload",
        files={"file": ("broken.pdf", io.BytesIO(b"not actually a pdf"), "application/pdf")},
        data={"name": "Broken"},
    )
    assert r.status_code == 422


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_upload_csv_via_generic_endpoint_gets_csv_origin(client):
    await _ready_account_with_luciel(client)
    r = await client.post(
        "/api/v1/admin/knowledge/sources/upload",
        files={"file": ("data.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv")},
        data={"name": "Data"},
    )
    assert r.status_code == 201
    assert r.json()["origin"] == "csv"


# --- rename + replace (raw knowledge view edit affordances, §3.2.2) --------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_rename_source(client):
    await _ready_account_with_luciel(client)
    ri = await client.post(
        "/api/v1/admin/knowledge/sources/paste",
        json={"name": "old name", "text": "starter engagement price 899"},
    )
    source_id = ri.json()["sourceId"]
    rr = await client.put(f"/api/v1/admin/knowledge/sources/{source_id}", json={"name": "new name"})
    assert rr.status_code == 200
    assert rr.json()["name"] == "new name"

    rl = await client.get("/api/v1/admin/knowledge/sources")
    assert any(s["sourceId"] == source_id and s["name"] == "new name" for s in rl.json())


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_rename_missing_source_404(client):
    await _ready_account_with_luciel(client)
    rr = await client.put(f"/api/v1/admin/knowledge/sources/{uuid.uuid4()}", json={"name": "x"})
    assert rr.status_code == 404


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_replace_source_reingests_content(client):
    await _ready_account_with_luciel(client)
    ri = await client.post(
        "/api/v1/admin/knowledge/sources/paste",
        json={"name": "doc", "text": "original stale content about widgets"},
    )
    source_id = ri.json()["sourceId"]

    rr = await client.post(
        f"/api/v1/admin/knowledge/sources/{source_id}/replace",
        files={
            "file": ("new.txt", io.BytesIO(b"corrected fresh content about gadgets"), "text/plain")
        },
    )
    assert rr.status_code == 200
    assert rr.json()["sourceId"] == source_id

    rc = await client.get(f"/api/v1/admin/knowledge/sources/{source_id}/chunks")
    joined = " ".join(c["text"] for c in rc.json())
    assert "gadgets" in joined
    assert "widgets" not in joined


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_replace_missing_source_404(client):
    await _ready_account_with_luciel(client)
    rr = await client.post(
        f"/api/v1/admin/knowledge/sources/{uuid.uuid4()}/replace",
        files={"file": ("new.txt", io.BytesIO(b"content"), "text/plain")},
    )
    assert rr.status_code == 404


# --- live-sync connectors: create / list / trigger (§3.2.3) ----------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_create_website_crawl_connection_is_immediately_connected(client):
    await _ready_account_with_luciel(client)
    r = await client.post(
        "/api/v1/admin/knowledge/sync-connections",
        json={"provider": "website_crawl", "crawlUrls": ["https://example.com/faq"]},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["provider"] == "website_crawl"
    assert body["status"] == "connected"
    assert body["nonSecretConfig"]["crawl_urls"] == ["https://example.com/faq"]

    rl = await client.get("/api/v1/admin/knowledge/sync-connections")
    assert any(c["connectionId"] == body["connectionId"] for c in rl.json())


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_create_website_crawl_without_urls_422(client):
    await _ready_account_with_luciel(client)
    r = await client.post(
        "/api/v1/admin/knowledge/sync-connections", json={"provider": "website_crawl"}
    )
    assert r.status_code == 422


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_create_oauth_provider_connection_returns_unconfigured_pending_authorize(client):
    await _ready_account_with_luciel(client)
    r = await client.post(
        "/api/v1/admin/knowledge/sync-connections", json={"provider": "google_drive"}
    )
    assert r.status_code == 201
    body = r.json()
    assert body["provider"] == "google_drive"
    # OAuth-class connect starts unconfigured — no token yet, real authorize/callback
    # exchange is a Connections-layer seam (see build_knowledge.md "live-blocked").
    assert body["status"] == "unconfigured"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_create_sync_connection_unknown_provider_422(client):
    await _ready_account_with_luciel(client)
    r = await client.post("/api/v1/admin/knowledge/sync-connections", json={"provider": "myspace"})
    assert r.status_code == 422


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_trigger_sync_missing_connection_404(client):
    await _ready_account_with_luciel(client)
    r = await client.post(f"/api/v1/admin/knowledge/sync-connections/{uuid.uuid4()}/sync")
    assert r.status_code == 404


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_trigger_sync_for_website_crawl_ingests_and_is_idempotent_when_unchanged(
    client, monkeypatch
):
    """End-to-end through the real admin API: create a website_crawl connection,
    trigger sync twice against a FAKE transport with unchanged content, and prove
    the second run reports no update (revision hash unchanged) rather than
    re-ingesting identical content on every manual trigger."""
    import httpx
    from app.knowledge import providers as providers_module

    class FakeTransport:
        async def get(self, url, *, headers=None, params=None):
            return httpx.Response(
                200, text="<p>Our hours are 9 to 5</p>", request=httpx.Request("GET", url)
            )

    original_factory = providers_module.provider_for_connection

    def patched_factory(connection, store):
        p = original_factory(connection, store)
        if isinstance(p, providers_module.WebsiteCrawlProvider):
            p._transport = FakeTransport()
        return p

    monkeypatch.setattr(providers_module, "provider_for_connection", patched_factory)
    # The admin router imported the name directly, so patch it there too.
    import app.api.v1.admin.knowledge as knowledge_api

    monkeypatch.setattr(knowledge_api, "provider_for_connection", patched_factory)

    await _ready_account_with_luciel(client)
    rc = await client.post(
        "/api/v1/admin/knowledge/sync-connections",
        json={"provider": "website_crawl", "crawlUrls": ["https://example.com/hours"]},
    )
    connection_id = rc.json()["connectionId"]

    r1 = await client.post(f"/api/v1/admin/knowledge/sync-connections/{connection_id}/sync")
    assert r1.status_code == 200
    assert r1.json()["added"] == ["https://example.com/hours"]

    r2 = await client.post(f"/api/v1/admin/knowledge/sync-connections/{connection_id}/sync")
    assert r2.status_code == 200
    assert r2.json()["added"] == []
    assert r2.json()["updated"] == []  # unchanged content -> no-op, not a re-ingest


# --- OAuth code->token callback (the previously-MISSING link) --------------- #
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_oauth_callback_exchanges_code_and_connects_then_sync_uses_stored_token(
    client, monkeypatch
):
    """Full round trip through the real admin API: create an OAuth-class
    sync-connection (starts 'unconfigured'), call the new oauth-callback with a
    fake transport standing in for the provider's token endpoint, assert the
    connection flips to 'connected' and a token is retrievable from the secret
    store, then trigger a sync (also against a fake transport) and assert the
    stored token is what the provider actually sends as the Authorization
    header — proving connect -> callback -> sync is wired end-to-end."""
    import app.api.v1.admin.knowledge as knowledge_api
    import httpx
    from app.connections.secrets import secret_ref_for

    class FakeTokenTransport:
        """Stands in for the real oauth2.googleapis.com/token endpoint — and, since
        round 6 WP-C, for the tokeninfo call the verifier makes with the new token
        to prove the grant carries the Drive read scope."""

        async def post(self, url, *, data=None, headers=None):
            assert url == "https://oauth2.googleapis.com/token"
            assert data["code"] == "auth-code-xyz"
            return httpx.Response(
                200,
                json={"access_token": "gd-tok-from-callback", "refresh_token": "gd-r"},
                request=httpx.Request("POST", url),
            )

        async def get(self, url, *, headers=None, params=None):
            assert url.startswith("https://oauth2.googleapis.com/tokeninfo")
            assert "access_token=gd-tok-from-callback" in url
            return httpx.Response(
                200,
                json={
                    "scope": "https://www.googleapis.com/auth/drive.readonly",
                    "expires_in": "3599",
                },
                request=httpx.Request("GET", url),
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(knowledge_api, "oauth_transport", lambda: FakeTokenTransport())

    from app.config import Settings
    from app.connections import registry as registry_module
    from app.knowledge import providers as providers_module

    configured = Settings(
        google_client_id="gcid",
        google_client_secret="gcsecret",
        google_redirect_uri="https://api.example.com/gd-callback",
    )
    monkeypatch.setattr(providers_module, "get_settings", lambda: configured)
    # The OAuth app credential is resolved through the provider REGISTRY now (Decision #6),
    # so the registry is the module whose settings read must see the test config.
    monkeypatch.setattr(registry_module, "get_settings", lambda: configured)

    admin_id, instance_id = await _ready_account_with_luciel_capturing_ids(client)
    rc = await client.post(
        "/api/v1/admin/knowledge/sync-connections", json={"provider": "google_drive"}
    )
    assert rc.status_code == 201
    conn_body = rc.json()
    assert conn_body["status"] == "unconfigured"
    connection_id = conn_body["connectionId"]

    rcb = await client.post(
        f"/api/v1/admin/knowledge/sync-connections/{connection_id}/oauth-callback",
        json={"code": "auth-code-xyz", "state": _state_from(conn_body)},
    )
    assert rcb.status_code == 200, rcb.text
    cb_body = rcb.json()
    assert cb_body["status"] == "connected"

    # The secret_ref convention is tenant-scoped (§3.8.3) — read it back via the
    # SAME secret store the app uses in mock mode to prove the write really
    # happened (not just that the endpoint returned 200). admin_id/instance_id
    # are never returned over the wire (§3.8.3), so we captured them directly
    # from account creation above instead of round-tripping the API for them.
    from app.connections.secrets import get_secret_store
    from app.db.session import tenant_session
    from app.models.connection import InstanceConnection
    from sqlalchemy import select

    store = get_secret_store()
    # Since knowledge connections became many-per-tenant (F092) each row owns its
    # own secret ref under the tenant prefix; the legacy per-type ref is not written.
    async with tenant_session(admin_id) as s:
        secret_ref = (
            await s.execute(
                select(InstanceConnection.secret_ref).where(
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.connection_id == uuid.UUID(connection_id),
                )
            )
        ).scalar_one()
    assert secret_ref and secret_ref.startswith(f"vm/{admin_id}/{instance_id}/")
    assert await store.get(secret_ref_for(admin_id, instance_id, "knowledge_source")) is None
    material = await store.get(secret_ref)
    assert material is not None
    assert material["access_token"] == "gd-tok-from-callback"

    # Now trigger a sync — the GoogleDriveProvider should read that exact stored
    # token and send it as the Authorization header (closing the loop).
    class FakeSyncTransport:
        def __init__(self):
            self.seen_auth = None

        async def get(self, url, *, headers=None, params=None):
            self.seen_auth = headers.get("Authorization") if headers else None
            return httpx.Response(200, json={"files": []}, request=httpx.Request("GET", url))

    fake_sync_transport = FakeSyncTransport()
    original_factory = providers_module.provider_for_connection

    def patched_factory(connection, secret_store):
        p = original_factory(connection, secret_store)
        if isinstance(p, providers_module.GoogleDriveProvider):
            p._transport = fake_sync_transport
        return p

    monkeypatch.setattr(providers_module, "provider_for_connection", patched_factory)
    monkeypatch.setattr(knowledge_api, "provider_for_connection", patched_factory)

    rs = await client.post(f"/api/v1/admin/knowledge/sync-connections/{connection_id}/sync")
    assert rs.status_code == 200, rs.text
    assert fake_sync_transport.seen_auth == "Bearer gd-tok-from-callback"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_oauth_callback_rejected_code_is_clean_409_not_500(client, monkeypatch):
    """A revoked/expired/invalid code from the provider must surface as a clean
    4xx via OAuthExchangeError, never an unhandled 500."""
    import app.api.v1.admin.knowledge as knowledge_api
    import httpx

    class FailingTokenTransport:
        async def post(self, url, *, data=None, headers=None):
            return httpx.Response(
                400, json={"error": "invalid_grant"}, request=httpx.Request("POST", url)
            )

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(knowledge_api, "oauth_transport", lambda: FailingTokenTransport())

    from app.config import Settings
    from app.connections import registry as registry_module
    from app.knowledge import providers as providers_module

    configured = Settings(
        hubspot_client_id="hcid",
        hubspot_client_secret="hcsecret",
        hubspot_redirect_uri="https://api.example.com/hs-callback",
    )
    monkeypatch.setattr(providers_module, "get_settings", lambda: configured)
    # The OAuth app credential is resolved through the provider REGISTRY now (Decision #6),
    # so the registry is the module whose settings read must see the test config.
    monkeypatch.setattr(registry_module, "get_settings", lambda: configured)

    await _ready_account_with_luciel(client)
    rc = await client.post("/api/v1/admin/knowledge/sync-connections", json={"provider": "hubspot"})
    connection_id = rc.json()["connectionId"]

    rcb = await client.post(
        f"/api/v1/admin/knowledge/sync-connections/{connection_id}/oauth-callback",
        json={"code": "expired-code", "state": _state_from(rc.json())},
    )
    assert rcb.status_code == 409
    assert "hubspot" in rcb.json()["message"].lower() or "exchange" in rcb.json()["message"].lower()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_trigger_sync_on_never_connected_oauth_connection_is_clean_409_not_500(client):
    """A sync-connection that never completed oauth-callback has no token on
    file. Triggering sync on it must surface a clean structured error (the
    ProviderUnavailable the provider layer raises), never an unhandled 500."""
    await _ready_account_with_luciel(client)
    rc = await client.post(
        "/api/v1/admin/knowledge/sync-connections", json={"provider": "google_drive"}
    )
    connection_id = rc.json()["connectionId"]
    assert rc.json()["status"] == "unconfigured"

    rs = await client.post(f"/api/v1/admin/knowledge/sync-connections/{connection_id}/sync")
    assert rs.status_code == 409
    assert rs.json()["message"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_oauth_callback_missing_connection_404(client):
    await _ready_account_with_luciel(client)
    r = await client.post(
        f"/api/v1/admin/knowledge/sync-connections/{uuid.uuid4()}/oauth-callback",
        json={"code": "whatever"},
    )
    assert r.status_code == 404


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_oauth_callback_on_non_oauth_provider_is_422(client):
    """website_crawl has no OAuth step — calling the callback on it is a client
    error (wrong connection type), not a crash."""
    await _ready_account_with_luciel(client)
    rc = await client.post(
        "/api/v1/admin/knowledge/sync-connections",
        json={"provider": "website_crawl", "crawlUrls": ["https://example.com/faq"]},
    )
    connection_id = rc.json()["connectionId"]
    r = await client.post(
        f"/api/v1/admin/knowledge/sync-connections/{connection_id}/oauth-callback",
        json={"code": "whatever"},
    )
    assert r.status_code == 422


async def _ready_account_with_luciel_capturing_ids(client) -> tuple[uuid.UUID, uuid.UUID]:
    """Same setup as ``_ready_account_with_luciel`` but also returns
    (admin_id, instance_id) so a test can verify the SecretStore write directly
    — those ids are deliberately never present in wire responses (§3.8.3)."""
    from app.auth import access

    email = f"kb-oauth-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "pw-123456", "captchaToken": "t"}
    )
    acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    r = await client.post(
        "/api/v1/admin/luciel", json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"}
    )
    instance_id = uuid.UUID(r.json()["instanceId"])
    return acct.id, instance_id
