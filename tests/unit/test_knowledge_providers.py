"""Unit tests for app.knowledge.providers (Architecture §3.2.3 live-sync connectors).

No real network call anywhere (Build Brief rule 2): OAuth-class providers
(Drive/Notion/HubSpot/Salesforce) are exercised against a FAKE HttpTransport that
returns canned JSON; WebsiteCrawlProvider is exercised against a FAKE transport
too (its real mode uses httpx directly, but that code path is not unit-tested here
— it's the one genuinely-offline-capable connector, proven by the integration test
in tests/integration/test_knowledge_sync.py using the crawl provider's `snapshot`
against a fake transport for determinism in CI).
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from app.config import Settings
from app.connections.registry import OAuthClientConfig
from app.knowledge.providers import (
    GoogleDriveProvider,
    HubSpotProvider,
    NotionProvider,
    OAuthExchangeError,
    ProviderUnavailable,
    SalesforceProvider,
    WebsiteCrawlProvider,
    exchange_code_for_token,
    oauth_client_config,
    provider_for_connection,
    store_oauth_token,
)


class FakeSecretStore:
    """Minimal in-memory SecretStore fake (Protocol: put/get/delete)."""

    def __init__(self, material: dict[str, dict[str, str]] | None = None) -> None:
        self._data = material or {}

    async def put(self, ref: str, material: dict[str, str]) -> None:
        self._data[ref] = material

    async def get(self, ref: str) -> dict[str, str] | None:
        return self._data.get(ref)

    async def delete(self, ref: str) -> None:
        self._data.pop(ref, None)


class FakeTransport:
    """Fake HttpTransport: returns a canned httpx.Response, records last call."""

    def __init__(self, json_body: dict | None = None, text_body: str | None = None) -> None:
        self._json_body = json_body
        self._text_body = text_body
        self.last_url: str | None = None
        self.last_headers: dict[str, str] | None = None
        self.last_params: dict[str, object] | None = None
        self.last_data: dict[str, object] | None = None

    async def get(self, url, *, headers=None, params=None):
        self.last_url = url
        self.last_headers = headers
        self.last_params = params
        if self._text_body is not None:
            return httpx.Response(200, text=self._text_body, request=httpx.Request("GET", url))
        return httpx.Response(200, json=self._json_body, request=httpx.Request("GET", url))

    async def post(self, url, *, data=None, headers=None):
        self.last_url = url
        self.last_headers = headers
        self.last_data = data
        status = getattr(self, "_status", 200)
        if self._text_body is not None:
            return httpx.Response(status, text=self._text_body, request=httpx.Request("POST", url))
        return httpx.Response(status, json=self._json_body, request=httpx.Request("POST", url))


def _secret_ref(admin_id: uuid.UUID, instance_id: uuid.UUID) -> str:
    from app.connections.secrets import secret_ref_for

    return secret_ref_for(admin_id, instance_id, "knowledge_source")


# --------------------------------------------------------------------------- #
# OAuth client config wiring — read from Settings/env, never hardcoded.
# --------------------------------------------------------------------------- #
def test_oauth_client_config_reads_from_settings(monkeypatch):
    from app.connections import registry as registry_module

    fresh = Settings(
        google_drive_client_id="cid",
        google_drive_client_secret="csecret",
        google_drive_redirect_uri="https://api.example.com/callback",
    )
    monkeypatch.setattr(registry_module, "get_settings", lambda: fresh)
    cfg = oauth_client_config("google_drive")
    assert cfg == OAuthClientConfig("cid", "csecret", "https://api.example.com/callback")
    assert cfg.configured


def test_oauth_client_config_empty_by_default_is_unconfigured(monkeypatch):
    from app.connections import registry as registry_module

    monkeypatch.setattr(registry_module, "get_settings", lambda: Settings())
    cfg = oauth_client_config("hubspot")
    assert cfg.configured is False


def test_oauth_client_config_unknown_provider_returns_empty(monkeypatch):
    from app.connections import registry as registry_module

    monkeypatch.setattr(registry_module, "get_settings", lambda: Settings())
    cfg = oauth_client_config("totally_unknown_provider")
    assert cfg == OAuthClientConfig("", "", "")


# --------------------------------------------------------------------------- #
# No-token-on-file -> ProviderUnavailable, never a silent empty snapshot.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_google_drive_provider_no_token_raises_provider_unavailable():
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    provider = GoogleDriveProvider(
        admin_id=admin_id, instance_id=instance_id, store=FakeSecretStore()
    )
    with pytest.raises(ProviderUnavailable):
        await provider.snapshot(uuid.uuid4())


# --------------------------------------------------------------------------- #
# Google Drive: delta-shaped snapshot against a fake transport.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_google_drive_provider_snapshot_maps_files(monkeypatch):
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    store = FakeSecretStore({_secret_ref(admin_id, instance_id): {"access_token": "tok-123"}})
    transport = FakeTransport(
        json_body={
            "files": [
                {
                    "id": "f1",
                    "name": "Playbook",
                    "modifiedTime": "2026-01-01T00:00:00Z",
                    "text": "alpha",
                },
                {
                    "id": "f2",
                    "name": "Trashed",
                    "modifiedTime": "2026-01-01T00:00:00Z",
                    "trashed": True,
                },
            ]
        }
    )
    provider = GoogleDriveProvider(
        admin_id=admin_id, instance_id=instance_id, store=store, transport=transport
    )
    snap = await provider.snapshot(uuid.uuid4())
    assert [i.external_id for i in snap.items] == ["f1"]  # trashed file excluded
    assert snap.items[0].revision == "2026-01-01T00:00:00Z"
    assert transport.last_headers["Authorization"] == "Bearer tok-123"


# --------------------------------------------------------------------------- #
# Notion: last_edited_time delta marker, archived pages excluded.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_notion_provider_snapshot_excludes_archived(monkeypatch):
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    store = FakeSecretStore({_secret_ref(admin_id, instance_id): {"access_token": "tok-abc"}})
    transport = FakeTransport(
        json_body={
            "results": [
                {"id": "p1", "title": "FAQ", "text": "hours", "last_edited_time": "t1"},
                {
                    "id": "p2",
                    "title": "Old",
                    "text": "stale",
                    "last_edited_time": "t0",
                    "archived": True,
                },
            ]
        }
    )
    provider = NotionProvider(
        admin_id=admin_id, instance_id=instance_id, store=store, transport=transport
    )
    snap = await provider.snapshot(uuid.uuid4())
    assert [i.external_id for i in snap.items] == ["p1"]
    assert transport.last_headers["Notion-Version"] == "2022-06-28"


# --------------------------------------------------------------------------- #
# HubSpot CRM-KB: updatedAt delta marker.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_hubspot_provider_snapshot_maps_articles():
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    store = FakeSecretStore({_secret_ref(admin_id, instance_id): {"access_token": "tok-hs"}})
    transport = FakeTransport(
        json_body={
            "results": [
                {"id": 1, "title": "Return policy", "postBody": "30 days", "updatedAt": "u1"},
            ]
        }
    )
    provider = HubSpotProvider(
        admin_id=admin_id, instance_id=instance_id, store=store, transport=transport
    )
    snap = await provider.snapshot(uuid.uuid4())
    assert snap.items[0].external_id == "1"
    assert snap.items[0].text == "30 days"
    assert snap.items[0].revision == "u1"
    assert provider.origin == "crm_kb"


# --------------------------------------------------------------------------- #
# Salesforce CRM-KB: LastModifiedDate delta marker, SOQL query call shape.
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_salesforce_provider_snapshot_maps_articles():
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    store = FakeSecretStore({_secret_ref(admin_id, instance_id): {"access_token": "tok-sf"}})
    transport = FakeTransport(
        json_body={
            "records": [
                {"Id": "ka01", "Title": "Warranty", "Summary": "1 year", "LastModifiedDate": "d1"},
            ]
        }
    )
    provider = SalesforceProvider(
        admin_id=admin_id,
        instance_id=instance_id,
        store=store,
        instance_url="https://example.my.salesforce.com",
        transport=transport,
    )
    snap = await provider.snapshot(uuid.uuid4())
    assert snap.items[0].external_id == "ka01"
    assert snap.items[0].text == "1 year"
    assert "example.my.salesforce.com" in transport.last_url
    assert provider.origin == "crm_kb"


# --------------------------------------------------------------------------- #
# Website crawl: genuinely offline (fake transport for determinism), diff via
# content-hash revision since there is no updatedAt/change-feed for a generic
# site (§3.2.3 crawl-sync note).
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_website_crawl_provider_extracts_visible_text_and_hashes_revision():
    html = (
        "<html><head><style>body{color:red}</style></head>"
        "<body><script>evil()</script><h1>Pricing</h1><p>Starter is $899.</p></body></html>"
    )
    transport = FakeTransport(text_body=html)
    provider = WebsiteCrawlProvider(urls=["https://example.com/pricing"], transport=transport)
    snap = await provider.snapshot(uuid.uuid4())
    assert len(snap.items) == 1
    item = snap.items[0]
    assert item.external_id == "https://example.com/pricing"
    assert "Pricing" in item.text
    assert "899" in item.text
    assert "evil()" not in item.text  # script stripped
    assert "color:red" not in item.text  # style stripped
    assert len(item.revision) == 64  # sha256 hex digest


@pytest.mark.asyncio
async def test_website_crawl_provider_revision_changes_when_content_changes():
    transport1 = FakeTransport(text_body="<p>Version one</p>")
    provider1 = WebsiteCrawlProvider(urls=["https://example.com/"], transport=transport1)
    snap1 = await provider1.snapshot(uuid.uuid4())

    transport2 = FakeTransport(text_body="<p>Version two, updated</p>")
    provider2 = WebsiteCrawlProvider(urls=["https://example.com/"], transport=transport2)
    snap2 = await provider2.snapshot(uuid.uuid4())

    assert snap1.items[0].revision != snap2.items[0].revision


@pytest.mark.asyncio
async def test_website_crawl_provider_revision_stable_when_content_unchanged():
    transport1 = FakeTransport(text_body="<p>Same content</p>")
    provider1 = WebsiteCrawlProvider(urls=["https://example.com/"], transport=transport1)
    snap1 = await provider1.snapshot(uuid.uuid4())

    transport2 = FakeTransport(text_body="<p>Same content</p>")
    provider2 = WebsiteCrawlProvider(urls=["https://example.com/"], transport=transport2)
    snap2 = await provider2.snapshot(uuid.uuid4())

    assert snap1.items[0].revision == snap2.items[0].revision


@pytest.mark.asyncio
async def test_website_crawl_provider_multiple_urls():
    # Two different fake transports simulate two independently-fetched pages —
    # the provider fetches each configured URL and reports one item per URL.
    calls = {"n": 0}
    bodies = ["<p>Page one</p>", "<p>Page two</p>"]

    class MultiPageTransport:
        async def get(self, url, *, headers=None, params=None):
            body = bodies[calls["n"]]
            calls["n"] += 1
            return httpx.Response(200, text=body, request=httpx.Request("GET", url))

    provider = WebsiteCrawlProvider(
        urls=["https://example.com/a", "https://example.com/b"], transport=MultiPageTransport()
    )
    snap = await provider.snapshot(uuid.uuid4())
    assert {i.external_id for i in snap.items} == {"https://example.com/a", "https://example.com/b"}
    assert calls["n"] == 2


# --------------------------------------------------------------------------- #
# provider_for_connection dispatch (brand -> concrete provider class).
# --------------------------------------------------------------------------- #
class _FakeConnection:
    def __init__(self, provider: str, non_secret_config: dict | None = None) -> None:
        self.provider = provider
        self.admin_id = uuid.uuid4()
        self.instance_id = uuid.uuid4()
        self.non_secret_config = non_secret_config


def test_provider_for_connection_dispatches_each_brand():
    store = FakeSecretStore()
    assert isinstance(
        provider_for_connection(_FakeConnection("google_drive"), store), GoogleDriveProvider
    )
    assert isinstance(provider_for_connection(_FakeConnection("notion"), store), NotionProvider)
    assert isinstance(provider_for_connection(_FakeConnection("hubspot"), store), HubSpotProvider)
    assert isinstance(
        provider_for_connection(_FakeConnection("salesforce"), store), SalesforceProvider
    )
    crawl = provider_for_connection(
        _FakeConnection("website_crawl", {"crawl_urls": ["https://a.com"]}), store
    )
    assert isinstance(crawl, WebsiteCrawlProvider)
    assert crawl._urls == ["https://a.com"]


def test_provider_for_connection_unknown_provider_raises():
    store = FakeSecretStore()
    with pytest.raises(ProviderUnavailable):
        provider_for_connection(_FakeConnection("carrier_pigeon"), store)


# --------------------------------------------------------------------------- #
# OAuth code -> token exchange (the link that was MISSING end-to-end: an
# authorize URL and a bearer-token reader existed, but nothing turned a
# provider `code` into a token and wrote it to the SecretStore). Exercised
# against the SAME FakeTransport seam as the read-side providers — no real
# network call anywhere (Build Brief rule 2).
# --------------------------------------------------------------------------- #
def _configured_settings(monkeypatch, provider_module):
    fresh = Settings(
        google_drive_client_id="gcid",
        google_drive_client_secret="gcsecret",
        google_drive_redirect_uri="https://api.example.com/gd-callback",
        hubspot_client_id="hcid",
        hubspot_client_secret="hcsecret",
        hubspot_redirect_uri="https://api.example.com/hs-callback",
        salesforce_consumer_key="scid",
        salesforce_consumer_secret="scsecret",
        salesforce_redirect_uri="https://api.example.com/sf-callback",
    )
    monkeypatch.setattr(provider_module, "get_settings", lambda: fresh)
    # The OAuth app-credential lookup reads settings through the provider registry, so
    # a configured-client test has to patch it there too.
    from app.connections import registry as registry_module

    monkeypatch.setattr(registry_module, "get_settings", lambda: fresh)
    return fresh


@pytest.mark.asyncio
async def test_exchange_code_for_token_google_drive_success(monkeypatch):
    from app.knowledge import providers as providers_module

    _configured_settings(monkeypatch, providers_module)
    transport = FakeTransport(json_body={"access_token": "gd-access-1", "refresh_token": "gd-r1"})
    material = await exchange_code_for_token("google_drive", code="authcode-1", transport=transport)
    assert material == {"access_token": "gd-access-1", "refresh_token": "gd-r1"}
    assert transport.last_url == "https://oauth2.googleapis.com/token"
    assert transport.last_data["code"] == "authcode-1"
    assert transport.last_data["client_id"] == "gcid"
    assert transport.last_data["client_secret"] == "gcsecret"


@pytest.mark.asyncio
async def test_exchange_code_for_token_salesforce_captures_instance_url(monkeypatch):
    from app.knowledge import providers as providers_module

    _configured_settings(monkeypatch, providers_module)
    transport = FakeTransport(
        json_body={
            "access_token": "sf-access-1",
            "instance_url": "https://myorg.my.salesforce.com",
        }
    )
    material = await exchange_code_for_token("salesforce", code="authcode-2", transport=transport)
    assert material["access_token"] == "sf-access-1"
    assert material["instance_url"] == "https://myorg.my.salesforce.com"
    assert transport.last_url == "https://login.salesforce.com/services/oauth2/token"


@pytest.mark.asyncio
async def test_exchange_code_for_token_unconfigured_client_raises_provider_unavailable(
    monkeypatch,
):
    from app.knowledge import providers as providers_module

    monkeypatch.setattr(providers_module, "get_settings", lambda: Settings())
    transport = FakeTransport(json_body={"access_token": "x"})
    with pytest.raises(ProviderUnavailable):
        await exchange_code_for_token("hubspot", code="c", transport=transport)


@pytest.mark.asyncio
async def test_exchange_code_for_token_revoked_or_expired_code_raises_clean_error(monkeypatch):
    """A revoked/expired/invalid code must surface a typed OAuthExchangeError —
    never a raw KeyError/HTTPStatusError — so the API layer can turn it into a
    clean 4xx instead of crashing (Build Brief requirement)."""
    from app.knowledge import providers as providers_module

    _configured_settings(monkeypatch, providers_module)
    transport = FakeTransport(json_body={"error": "invalid_grant"})
    transport._status = 400
    with pytest.raises(OAuthExchangeError):
        await exchange_code_for_token("google_drive", code="expired-code", transport=transport)


@pytest.mark.asyncio
async def test_exchange_code_for_token_error_shaped_200_still_raises(monkeypatch):
    """Some providers return HTTP 200 with an error shape in the body — must
    still be treated as a clean failure, not a fabricated token."""
    from app.knowledge import providers as providers_module

    _configured_settings(monkeypatch, providers_module)
    transport = FakeTransport(json_body={"error": "invalid_grant", "error_description": "expired"})
    with pytest.raises(OAuthExchangeError):
        await exchange_code_for_token("hubspot", code="bad", transport=transport)


@pytest.mark.asyncio
async def test_store_oauth_token_writes_tenant_scoped_secret_ref_reusable_by_sync(monkeypatch):
    """Proves the closing link: material written by ``store_oauth_token`` is
    immediately readable by the SAME ``secret_ref`` convention the sync-time
    ``GoogleDriveProvider`` reads from — i.e. a callback-stored token actually
    powers the next sync run with no extra wiring."""
    admin_id, instance_id = uuid.uuid4(), uuid.uuid4()
    store = FakeSecretStore()
    ref = await store_oauth_token(
        store,
        admin_id=admin_id,
        instance_id=instance_id,
        material={"access_token": "tok-from-callback"},
    )
    assert ref == _secret_ref(admin_id, instance_id)

    transport = FakeTransport(json_body={"files": []})
    provider = GoogleDriveProvider(
        admin_id=admin_id, instance_id=instance_id, store=store, transport=transport
    )
    await provider.snapshot(uuid.uuid4())
    assert transport.last_headers["Authorization"] == "Bearer tok-from-callback"
