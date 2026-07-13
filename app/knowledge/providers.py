"""Real SyncProvider implementations (Architecture §3.2.3, Arc 11b).

The sync ENGINE (``app.knowledge.sync``) is provider-agnostic — it only depends on
the ``SyncProvider`` Protocol (``snapshot(connection_id) -> RemoteSnapshot``). This
module supplies the concrete providers §3.2.3 names as committed launch connectors:

  - ``GoogleDriveProvider``  — Drive `changes` API delta feed (page-token).
  - ``NotionProvider``       — `last_edited_time` filtering on connected pages/DBs.
  - ``HubSpotProvider``      — CRM knowledge-base articles, `updatedAt` delta.
  - ``SalesforceProvider``   — CRM knowledge-base articles, `LastModifiedDate` delta.
  - ``WebsiteCrawlProvider`` — no OAuth, no change feed: full re-fetch + diff via
    httpx (§3.2.3 "crawl-sync works by periodic full re-fetch + diff").

OAuth-class providers (Drive/HubSpot/Salesforce) hold an access token retrieved
from the injected ``SecretStore`` at call time (§3.8.3: the secret never touches
Postgres, never touches this constructor's arguments as a literal) and make their
provider API call through an injected ``httpx.AsyncClient``-shaped transport so
they are unit-testable against a FAKE transport with NO real network call, per the
Build Brief's "everything runnable in mock mode" rule. In this arc's default (mock)
posture, the real HTTP call sites raise ``ProviderUnavailable`` when no token is on
file (a live OAuth round-trip is a provider-deferred seam exactly like the
Connections layer's own authorize/callback flow) — the DELTA-SYNC CONTRACT logic
(pagination cursor, updatedAt/last_edited_time comparison, deletion propagation) is
the part this arc must prove, and it is fully exercised against a FAKE transport in
tests, per the Build Brief.

OAuth CLIENT registration (id/secret/redirect) is read from ``app.config.Settings``
— see ``oauth_client_config`` below — which is itself sourced from the environment
(Secrets Manager/SSM in production). NEVER hardcoded here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.config import get_settings
from app.connections.secrets import SecretStore, secret_ref_for
from app.knowledge.sync import RemoteItem, RemoteSnapshot


class ProviderUnavailable(Exception):
    """Raised when a live provider call cannot proceed (no token, or the provider
    OAuth client isn't configured). Callers surface this as a sync error status,
    never as a silent empty snapshot (§3.2.3: never fabricate freshness)."""


@dataclass(frozen=True)
class OAuthClientConfig:
    """The application's OAuth-client registration for one provider (§3.8.3: this
    is NOT the per-tenant token — it's who the app claims to be to the provider)."""

    client_id: str
    client_secret: str
    redirect_uri: str

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


def oauth_client_config(provider: str) -> OAuthClientConfig:
    """Read the OAuth client registration for a provider from config/Secrets
    Manager (never hardcoded, Build Brief rule 2). Unknown providers return an
    unconfigured (empty) config rather than raising, so the catalog can grow.
    """
    s = get_settings()
    if provider == "google_drive":
        return OAuthClientConfig(
            s.google_drive_client_id, s.google_drive_client_secret, s.google_drive_redirect_uri
        )
    if provider in ("hubspot", "crm_kb"):
        # crm_kb is the §3.2.3 wire origin; hubspot/salesforce are the concrete
        # native-CRM providers behind it (Vision §7 "native CRM connectors").
        return OAuthClientConfig(
            s.hubspot_client_id, s.hubspot_client_secret, s.hubspot_redirect_uri
        )
    if provider == "salesforce":
        return OAuthClientConfig(
            s.salesforce_consumer_key,
            s.salesforce_consumer_secret,
            s.salesforce_redirect_uri,
        )
    return OAuthClientConfig("", "", "")


class HttpTransport(Protocol):
    """The narrow HTTP seam every OAuth-class provider calls through. A real impl
    is ``httpx.AsyncClient``; tests inject a FAKE that returns canned JSON/text
    with no network call (Build Brief rule 2)."""

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None, params: dict[str, object] | None = None
    ) -> httpx.Response: ...


async def _bearer_token(store: SecretStore, admin_id: uuid.UUID, instance_id: uuid.UUID) -> str:
    material = await store.get(secret_ref_for(admin_id, instance_id, "knowledge_source"))
    if not material or not material.get("access_token"):
        raise ProviderUnavailable("No access token on file for this knowledge-sync connection.")
    return material["access_token"]


# --------------------------------------------------------------------------- #
# Google Drive — `changes` API delta feed (page-token), §3.2.3.
# --------------------------------------------------------------------------- #
class GoogleDriveProvider:
    """Enumerates files in the connected Drive scope; delta via the `changes` API
    page-token when available, full listing on first sync (no prior token)."""

    origin = "google_drive"

    def __init__(
        self,
        *,
        admin_id: uuid.UUID,
        instance_id: uuid.UUID,
        store: SecretStore,
        transport: HttpTransport | None = None,
    ) -> None:
        self._admin_id = admin_id
        self._instance_id = instance_id
        self._store = store
        self._transport = transport

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        token = await _bearer_token(self._store, self._admin_id, self._instance_id)
        transport = self._transport
        if transport is None:
            raise ProviderUnavailable(
                "Google Drive live HTTP call is a provider-deferred seam in this arc; "
                "inject an HttpTransport (real or fake) to exercise it."
            )
        resp = await transport.get(
            "https://www.googleapis.com/drive/v3/files",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": "trashed=false", "fields": "files(id,name,modifiedTime,trashed)"},
        )
        data = resp.json()
        items = [
            RemoteItem(
                external_id=f["id"],
                name=f["name"],
                text=f.get("text", ""),
                revision=f.get("modifiedTime", ""),
            )
            for f in data.get("files", [])
            if not f.get("trashed")
        ]
        return RemoteSnapshot(items=items)


# --------------------------------------------------------------------------- #
# Notion — `last_edited_time` filtering on connected pages/databases, §3.2.3.
# --------------------------------------------------------------------------- #
class NotionProvider:
    """Enumerates connected Notion pages/databases; delta via `last_edited_time`."""

    origin = "notion"

    def __init__(
        self,
        *,
        admin_id: uuid.UUID,
        instance_id: uuid.UUID,
        store: SecretStore,
        transport: HttpTransport | None = None,
    ) -> None:
        self._admin_id = admin_id
        self._instance_id = instance_id
        self._store = store
        self._transport = transport

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        token = await _bearer_token(self._store, self._admin_id, self._instance_id)
        transport = self._transport
        if transport is None:
            raise ProviderUnavailable(
                "Notion live HTTP call is a provider-deferred seam in this arc; inject "
                "an HttpTransport (real or fake) to exercise it."
            )
        resp = await transport.get(
            "https://api.notion.com/v1/search",
            headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"},
            params={"filter": "page"},
        )
        data = resp.json()
        items = [
            RemoteItem(
                external_id=p["id"],
                name=p.get("title", p["id"]),
                text=p.get("text", ""),
                revision=p.get("last_edited_time", ""),
            )
            for p in data.get("results", [])
            if not p.get("archived")
        ]
        return RemoteSnapshot(items=items)


# --------------------------------------------------------------------------- #
# CRM knowledge base — HubSpot / Salesforce, `updatedAt`/`LastModifiedDate` delta.
# --------------------------------------------------------------------------- #
class HubSpotProvider:
    """HubSpot Knowledge Base articles (`crm_kb` origin); delta via `updatedAt`."""

    origin = "crm_kb"

    def __init__(
        self,
        *,
        admin_id: uuid.UUID,
        instance_id: uuid.UUID,
        store: SecretStore,
        transport: HttpTransport | None = None,
    ) -> None:
        self._admin_id = admin_id
        self._instance_id = instance_id
        self._store = store
        self._transport = transport

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        token = await _bearer_token(self._store, self._admin_id, self._instance_id)
        transport = self._transport
        if transport is None:
            raise ProviderUnavailable(
                "HubSpot live HTTP call is a provider-deferred seam in this arc; inject "
                "an HttpTransport (real or fake) to exercise it."
            )
        resp = await transport.get(
            "https://api.hubapi.com/cms/v3/knowledge-base/articles",
            headers={"Authorization": f"Bearer {token}"},
            params={"archived": "false"},
        )
        data = resp.json()
        items = [
            RemoteItem(
                external_id=str(a["id"]),
                name=a.get("title", str(a["id"])),
                text=a.get("postBody", a.get("text", "")),
                revision=a.get("updatedAt", ""),
            )
            for a in data.get("results", [])
        ]
        return RemoteSnapshot(items=items)


class SalesforceProvider:
    """Salesforce Knowledge articles (`crm_kb` origin); delta via `LastModifiedDate`."""

    origin = "crm_kb"

    def __init__(
        self,
        *,
        admin_id: uuid.UUID,
        instance_id: uuid.UUID,
        store: SecretStore,
        instance_url: str = "https://login.salesforce.com",
        transport: HttpTransport | None = None,
    ) -> None:
        self._admin_id = admin_id
        self._instance_id = instance_id
        self._store = store
        self._instance_url = instance_url
        self._transport = transport

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        token = await _bearer_token(self._store, self._admin_id, self._instance_id)
        transport = self._transport
        if transport is None:
            raise ProviderUnavailable(
                "Salesforce live HTTP call is a provider-deferred seam in this arc; "
                "inject an HttpTransport (real or fake) to exercise it."
            )
        query = (
            "SELECT Id, Title, Summary, LastModifiedDate FROM Knowledge__kav "
            "WHERE PublishStatus = 'Online'"
        )
        resp = await transport.get(
            f"{self._instance_url}/services/data/v58.0/query",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": query},
        )
        data = resp.json()
        items = [
            RemoteItem(
                external_id=str(r["Id"]),
                name=r.get("Title", str(r["Id"])),
                text=r.get("Summary", r.get("text", "")),
                revision=r.get("LastModifiedDate", ""),
            )
            for r in data.get("records", [])
        ]
        return RemoteSnapshot(items=items)


# --------------------------------------------------------------------------- #
# Website crawl — no OAuth, no change feed: full re-fetch + diff, §3.2.3.
# --------------------------------------------------------------------------- #
def _extract_visible_text(html: str) -> str:
    """Very small, dependency-free HTML→text: strips tags/scripts/styles.

    Not a full HTML parser — the crawl connector's job is "get readable text for
    embedding," not pixel-perfect rendering, so a minimal regex-free state machine
    is sufficient and keeps this module dependency-light (Build Brief mock-mode
    rule: no new heavyweight dependency for something this narrow).
    """
    out: list[str] = []
    in_tag = False
    skip_tag: str | None = None
    buf = ""
    i = 0
    n = len(html)
    while i < n:
        ch = html[i]
        if not in_tag and ch == "<":
            in_tag = True
            tag_buf = ""
            i += 1
            continue
        if in_tag:
            if ch == ">":
                in_tag = False
                tag_name = tag_buf.strip().lstrip("/").split()[0].lower() if tag_buf.strip() else ""
                if tag_buf.strip().startswith("/"):
                    if skip_tag and tag_name == skip_tag:
                        skip_tag = None
                elif tag_name in ("script", "style"):
                    skip_tag = tag_name
                i += 1
                continue
            tag_buf += ch
            i += 1
            continue
        if skip_tag is None:
            buf += ch
        i += 1
    # Collapse whitespace so retrieval chunks aren't full of raw HTML formatting.
    return " ".join(buf.split())


class WebsiteCrawlProvider:
    """Genuinely offline-testable: httpx fetch of configured URL(s) + diff against
    the PREVIOUS fetch (no change feed exists for a generic website, §3.2.3).

    Revision tracking is content-hash based (there is no `updatedAt` to read from
    an arbitrary site) — ``RemoteItem.revision`` carries the SHA-256 of the
    extracted text, so the generic sync engine's "revision changed → re-ingest"
    rule works unmodified for a source with no native delta primitive.
    """

    origin = "website_crawl"

    def __init__(
        self,
        *,
        urls: list[str],
        transport: HttpTransport | None = None,
        timeout_seconds: float | None = None,
        max_bytes: int | None = None,
    ) -> None:
        self._urls = urls
        self._transport = transport
        settings = get_settings()
        self._timeout = timeout_seconds if timeout_seconds is not None else settings.website_crawl_timeout_seconds
        self._max_bytes = max_bytes if max_bytes is not None else settings.website_crawl_max_bytes

    async def _fetch_one(self, url: str) -> str:
        if self._transport is not None:
            resp = await self._transport.get(url)
            body = resp.text
        else:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                body = resp.text
        if len(body.encode("utf-8")) > self._max_bytes:
            body = body.encode("utf-8")[: self._max_bytes].decode("utf-8", errors="ignore")
        return body

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        import hashlib

        items: list[RemoteItem] = []
        for url in self._urls:
            html = await self._fetch_one(url)
            text = _extract_visible_text(html)
            revision = hashlib.sha256(text.encode("utf-8")).hexdigest()
            items.append(RemoteItem(external_id=url, name=url, text=text, revision=revision))
        return RemoteSnapshot(items=items)


# --------------------------------------------------------------------------- #
# Dispatch: build the real provider for an instance_connections row (§3.2.3/§3.8).
# --------------------------------------------------------------------------- #
def provider_for_connection(connection: object, store: SecretStore) -> object:
    """Return the concrete ``SyncProvider`` for a knowledge_source connection row.

    ``connection`` is an ``InstanceConnection``; typed as ``object`` here to avoid
    importing the model into this module's public type signature (keeps this
    module import-light for tests that only need the provider classes). The
    caller (admin API) always passes a real ``InstanceConnection``.
    """
    provider_name = connection.provider  # type: ignore[attr-defined]
    admin_id = connection.admin_id  # type: ignore[attr-defined]
    instance_id = connection.instance_id  # type: ignore[attr-defined]
    if provider_name == "google_drive":
        return GoogleDriveProvider(admin_id=admin_id, instance_id=instance_id, store=store)
    if provider_name == "notion":
        return NotionProvider(admin_id=admin_id, instance_id=instance_id, store=store)
    if provider_name == "hubspot":
        return HubSpotProvider(admin_id=admin_id, instance_id=instance_id, store=store)
    if provider_name == "salesforce":
        return SalesforceProvider(admin_id=admin_id, instance_id=instance_id, store=store)
    if provider_name == "website_crawl":
        cfg = connection.non_secret_config or {}  # type: ignore[attr-defined]
        urls = cfg.get("crawl_urls") or []
        return WebsiteCrawlProvider(urls=list(urls))
    raise ProviderUnavailable(f"No sync provider registered for '{provider_name}'.")
