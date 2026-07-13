"""Contract tests for the KNOWLEDGE additions in this build (Architecture §3.2.2 /
§3.2.3): CSV import path, rename + replace (raw knowledge view edit affordances),
and the live-sync connector admin API (create / list / trigger).
"""

from __future__ import annotations

import io
import uuid

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


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
