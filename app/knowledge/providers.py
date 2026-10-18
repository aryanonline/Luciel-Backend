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
Build Brief's "everything runnable in mock mode" rule. Which transport that is comes
from :func:`live_http_transport`: a real :class:`HttpxTransport` in live mode, ``None``
everywhere else, so mock mode keeps the fakes and the DELTA-SYNC CONTRACT logic
(pagination cursor, updatedAt/last_edited_time comparison, deletion propagation) stays
fully exercised with no network call.

Until P1-2 nothing bound the live half at all. ``provider_for_connection`` constructed
every provider with ``transport=None``, so an admin who completed a real Drive consent
got a connection that synced nothing and reported itself as a "provider-deferred seam
in this arc" — a description of the code's own missing wiring, surfaced to the customer
as if it were a property of the provider.

OAuth CLIENT registration (id/secret/redirect) is read from ``app.config.Settings`` via
``connections.registry.oauth_client_config`` — the registry names the settings fields
per provider, and those are themselves sourced from the environment (Secrets
Manager/SSM in production). NEVER hardcoded here.
"""

from __future__ import annotations

import base64
import re
import uuid
from typing import Protocol
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx

from app.config import get_settings
from app.connections import registry, sandbox
from app.connections.registry import oauth_client_config
from app.connections.secrets import SecretStore, secret_ref_for
from app.knowledge import parsers, scope
from app.knowledge.sync import RemoteItem, RemoteSnapshot, SyncProvider


class ProviderUnavailable(Exception):
    """Raised when a live provider call cannot proceed (no token, or the provider
    OAuth client isn't configured). Callers surface this as a sync error status,
    never as a silent empty snapshot (§3.2.3: never fabricate freshness)."""


class HttpTransport(Protocol):
    """The narrow HTTP seam every OAuth-class provider calls through. A real impl
    is ``httpx.AsyncClient``; tests inject a FAKE that returns canned JSON/text
    with no network call (Build Brief rule 2)."""

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, object] | None = None,
    ) -> httpx.Response: ...

    async def post(
        self,
        url: str,
        *,
        data: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        json: dict[str, object] | None = None,
    ) -> httpx.Response: ...


class HttpxTransport:
    """The real backend behind :class:`HttpTransport` (P1-2, ruling 4).

    Every OAuth-class provider was written against the seam and NOTHING ever bound a
    real implementation to it, so in live mode each one raised "inject an HttpTransport"
    — a connected Drive that never synced, reported as a provider-deferred seam rather
    than as the missing wiring it was.

    A client per request rather than a shared pooled one: a sync is a handful of calls
    minutes apart, so there is no pool to amortize, and a module-global AsyncClient
    would outlive the event loop it was created on (the sweep and the request path do
    not share one).

    A non-2xx is raised as :class:`ProviderUnavailable` here rather than handed back as
    a Response, because the callers all go straight to ``resp.json()``: a revoked token
    would otherwise surface as a KeyError deep in a list comprehension instead of the
    clean "reconnect this source" the sync status is meant to show.
    """

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._timeout = timeout

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, object] | None = None,
    ) -> httpx.Response:
        return await self._send("GET", url, headers=headers, params=params)

    async def post(
        self,
        url: str,
        *,
        data: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        json: dict[str, object] | None = None,
    ) -> httpx.Response:
        return await self._send("POST", url, headers=headers, data=data, json=json)

    async def _send(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.request(method, url, **kwargs)  # type: ignore[arg-type]
                resp.raise_for_status()
                return resp
        except httpx.HTTPStatusError as exc:
            raise ProviderUnavailable(
                f"{urlparse(url).hostname} refused the request ({exc.response.status_code}); "
                "the connection may need to be re-authorized."
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"{urlparse(url).hostname} is unreachable: {exc}") from exc


def live_http_transport() -> HttpTransport | None:
    """The real transport in live mode, ``None`` in every other mode (ruling 4).

    ``None`` is what the existing "inject an HttpTransport" guards read, so mock mode
    keeps behaving exactly as it did and every test that injects a fake is unaffected.
    """
    return HttpxTransport() if get_settings().integration_mode == "live" else None


async def _token_material(
    store: SecretStore,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    secret_ref: str | None = None,
) -> dict[str, str]:
    # Each knowledge connection reads ITS OWN ref (F092); the legacy shared ref is the
    # fallback for rows that predate per-connection refs and for direct constructions.
    ref = secret_ref or secret_ref_for(admin_id, instance_id, "knowledge_source")
    material = await store.get(ref)
    if not material or not material.get("access_token"):
        raise ProviderUnavailable("No access token on file for this knowledge-sync connection.")
    return material


async def _bearer_token(
    store: SecretStore,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    secret_ref: str | None = None,
) -> str:
    return (await _token_material(store, admin_id, instance_id, secret_ref))["access_token"]


# --------------------------------------------------------------------------- #
# OAuth CODE -> TOKEN exchange (§3.8.3 / Architecture §3.2.3): completes the
# authorize-URL half of the connect flow that ``connections.service.start_connection``
# already returns. This is the piece that was MISSING end-to-end: there was an
# authorize URL and a bearer-token consumer, but nothing that turned a provider's
# `code` into an access token and wrote it to the SecretStore as a tenant-scoped
# secret_ref. Every provider's token endpoint is a plain `POST application/x-www-
# form-urlencoded` -> JSON call, so ONE function covers all three OAuth-class
# providers (google_drive/hubspot/salesforce) — only the endpoint URL and the
# extra `instance_url` Salesforce returns differ.
#
# The real provider round-trip (POST to accounts.google.com / hubspot / salesforce
# login) is the one genuinely-external step that cannot be exercised in this arc
# without a real consent click-through — exactly like the authorize URL redirect
# itself. It is exercised here against the SAME FAKE ``HttpTransport`` seam the
# read-side providers already use, so the whole code->token->secret_ref->sync
# chain is provably wired end-to-end in mock mode; only the provider's own HTTP
# round-trip is the documented manual seam (see gap_oauth.md).
# --------------------------------------------------------------------------- #
def _authorize_spec(provider: str) -> registry.ProviderAuthorize | None:
    """A provider's exchange endpoint, extra token fields, and long-lived upgrade.

    Registered as configuration rather than module-local maps, so a newly registered
    provider is exchangeable without editing this file."""
    spec = registry.get_provider(provider)
    return spec.authorize if spec is not None else None


class OAuthExchangeError(Exception):
    """Raised when the provider rejects the code (revoked/expired/invalid grant).

    Callers (the oauth-callback admin handler) MUST turn this into a clean 4xx
    ApiError, never let it propagate as an unhandled 500 (Build Brief rule: a
    revoked/expired credential surfaces a clean error, not a crash)."""


async def exchange_code_for_token(
    provider: str,
    *,
    code: str,
    transport: HttpTransport,
    code_verifier: str | None = None,
) -> dict[str, str]:
    """Exchange an OAuth ``code`` for an access token via the provider's token
    endpoint, through the SAME ``HttpTransport`` seam ``snapshot()`` uses (a real
    impl is ``httpx.AsyncClient``; tests inject a FAKE — no real network call).

    ``code_verifier`` completes the PKCE exchange for providers whose authorize
    request carried a challenge (§5.1); omitting it for those providers makes the
    provider reject the code. Non-PKCE providers must not receive it.

    Returns the material dict to hand to ``SecretStore.put`` verbatim (never
    logged, never returned over the wire — §3.8.3 rule 4). Raises
    ``OAuthExchangeError`` on a non-2xx / error-shaped response (revoked,
    expired, or invalid code) so the caller can surface a clean 4xx rather than
    crash on a KeyError/HTTPStatusError.
    """
    cfg = oauth_client_config(provider)
    if not cfg.configured:
        raise ProviderUnavailable(
            f"OAuth client for '{provider}' is not configured (missing client_id/secret)."
        )
    authorize = _authorize_spec(provider)
    endpoint = authorize.exchange_endpoint if authorize is not None else None
    if authorize is None or endpoint is None:
        raise ProviderUnavailable(f"No token endpoint registered for provider '{provider}'.")

    body: dict[str, object] = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg.redirect_uri,
    }
    headers = {"Accept": "application/json"}
    # Client auth per the registry's declared style (registry.ProviderAuthorize):
    # client_secret_post (the OAuth default) puts the credentials in the body;
    # client_secret_basic (Notion — it 401s body credentials) puts them in an HTTP
    # Basic header and the body stays credential-free.
    if authorize.exchange_client_auth == "basic":
        raw = f"{cfg.client_id}:{cfg.client_secret}".encode()
        headers["Authorization"] = "Basic " + base64.b64encode(raw).decode()
    else:
        body["client_id"] = cfg.client_id
        body["client_secret"] = cfg.client_secret
    if code_verifier:
        body["code_verifier"] = code_verifier

    # Body encoding likewise declared per provider: Notion's token endpoint takes
    # JSON; everyone else takes the form encoding RFC 6749 specifies.
    if authorize.exchange_body == "json":
        resp = await transport.post(endpoint, json=body, headers=headers)
    else:
        resp = await transport.post(endpoint, data=body, headers=headers)
    if resp.status_code >= 400:
        raise OAuthExchangeError(
            f"{provider} token exchange failed (HTTP {resp.status_code}): "
            f"code likely revoked, expired, or invalid."
        )
    data = resp.json()
    if "error" in data or not data.get("access_token"):
        reason = data.get("error", "no access_token in response")
        raise OAuthExchangeError(f"{provider} token exchange rejected: {reason}")
    material: dict[str, str] = {"access_token": str(data["access_token"])}
    if data.get("refresh_token"):
        material["refresh_token"] = str(data["refresh_token"])
    # Always capture the token lifetime when the provider states one: it is the
    # expiry HINT the proactive-refresh worker (§3.8.5, app/connections/refresh)
    # keys on. Before this, only providers that listed expires_in in
    # token_fields kept it — i.e. none — so no non-Outlook connection ever got
    # an expiry hint and the refresh branch never fired for them.
    if data.get("expires_in"):
        material["expires_in"] = str(data["expires_in"])
    for extra in authorize.token_fields:
        if data.get(extra):
            material[extra] = str(data[extra])

    if authorize.long_lived_endpoint and authorize.long_lived_grant_type:
        long_lived_token, long_lived_expires_in = await _upgrade_to_long_lived(
            provider,
            authorize,
            client_id=cfg.client_id,
            client_secret=cfg.client_secret,
            short_lived=material["access_token"],
            transport=transport,
        )
        material["access_token"] = long_lived_token
        # The LONG-lived expiry is the one that matters (~60 days); keeping the
        # short-lived token's ~1h value would make the sweep refresh constantly.
        if long_lived_expires_in:
            material["expires_in"] = long_lived_expires_in
        else:
            material.pop("expires_in", None)
    return material


async def _upgrade_to_long_lived(
    provider: str,
    authorize: registry.ProviderAuthorize,
    *,
    client_id: str,
    client_secret: str,
    short_lived: str,
    transport: HttpTransport,
) -> tuple[str, str | None]:
    """Trade a short-lived token for a long-lived one (Meta family, §3.8.3).

    Returns ``(token, expires_in_or_None)`` — the long-lived expiry (~60 days) is
    the hint the proactive-refresh worker needs; the caller replaces the
    short-lived ~1h value with it. Raises ``OAuthExchangeError`` rather than
    falling back to the short-lived token: the fallback would look like a
    successful connect and then break about an hour later, with nothing in the
    connection's status to explain why.

    The two Meta grants name their parameters differently (item 14): Facebook's
    ``fb_exchange_token`` grant wants ``client_id`` + the short token under
    ``fb_exchange_token``; Instagram's ``ig_exchange_token`` wants it under
    ``access_token`` with no client_id.
    """
    grant = str(authorize.long_lived_grant_type)
    params: dict[str, object] = {"grant_type": grant, "client_secret": client_secret}
    if grant == "fb_exchange_token":  # noqa: S105 — a grant type, not a secret
        params["client_id"] = client_id
        params["fb_exchange_token"] = short_lived
    else:
        params["access_token"] = short_lived
    resp = await transport.get(
        str(authorize.long_lived_endpoint),
        params=params,
        headers={"Accept": "application/json"},
    )
    if resp.status_code >= 400:
        raise OAuthExchangeError(
            f"{provider} long-lived token exchange failed (HTTP {resp.status_code})."
        )
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise OAuthExchangeError(
            f"{provider} long-lived token exchange returned no access_token "
            f"({data.get('error', 'no reason given')})."
        )
    expires_in = str(data["expires_in"]) if data.get("expires_in") else None
    return str(token), expires_in


async def store_oauth_token(
    store: SecretStore,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    material: dict[str, str],
    connection_type: str = "knowledge_source",
    staged: bool = False,
    secret_ref: str | None = None,
) -> str:
    """Write exchanged token material to the tenant-scoped secret_ref (§3.8.3).

    ``secret_ref`` names the row's own pointer for per-connection types (F092); the
    ``connection_type`` convention is the fallback for every single-active type.

    Uses the SAME ``secret_ref_for(admin_id, instance_id, connection_type)``
    convention the readers consume, so a token written here is immediately usable
    with no other wiring — this is the link that closes connect -> callback -> use.
    ``connection_type`` defaults to knowledge sync, and is ``channel_auth`` for the
    Meta channel callback (Decision #7).
    """
    ref = secret_ref or secret_ref_for(admin_id, instance_id, connection_type)
    if staged:
        # A STAGED write (2026-09-05 audit F050): the live credential at ``ref`` keeps
        # serving until the callback has verified the new one and cleared the
        # destination check; ``promote_staged_token`` then makes it live. Writing
        # straight to ``ref`` used to replace a working token with an unverified one
        # — a failed verify left the row "connected" on the old provider with the
        # new account's token behind it.
        ref = staged_secret_ref(ref)
    await store.put(ref, material)
    return ref


def staged_secret_ref(ref: str) -> str:
    """Where a not-yet-verified credential waits. Under the same ``vm/{admin}/`` prefix
    so IAM scoping and the cascade prefix sweep cover it."""
    return f"{ref}/staged"


async def promote_staged_token(
    store: SecretStore,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    connection_type: str,
    secret_ref: str | None = None,
) -> bool:
    """Make the staged credential the live one. False if nothing was staged."""
    ref = secret_ref or secret_ref_for(admin_id, instance_id, connection_type)
    staged = staged_secret_ref(ref)
    material = await store.get(staged)
    if material is None:
        return False
    await store.put(ref, material)
    await store.delete(staged)
    return True


async def discard_staged_token(
    store: SecretStore,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    connection_type: str,
    secret_ref: str | None = None,
) -> bool:
    """Drop a staged credential that failed verification; the live one is untouched."""
    ref = staged_secret_ref(secret_ref or secret_ref_for(admin_id, instance_id, connection_type))
    return await store.delete(ref)


# --------------------------------------------------------------------------- #
# Google Drive — `changes` API delta feed (page-token), §3.2.3.
# --------------------------------------------------------------------------- #
#: Google-native document kinds and the plain export each one is read as (F097).
DRIVE_EXPORT_MIME: dict[str, str] = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}
#: Listing pages read per sync (100 files each) — a bound, not a target.
DRIVE_MAX_PAGES = 50
#: Binary files above this are skipped rather than pulled through the parser.
DRIVE_MAX_FILE_BYTES = 25_000_000


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
        scope_ids: list[str] | None = None,
        secret_ref: str | None = None,
    ) -> None:
        self._admin_id = admin_id
        self._instance_id = instance_id
        self._store = store
        self._transport = transport
        self._secret_ref = secret_ref
        # Selected folder ids (Decision #9); empty = the whole connected account.
        self._scope_ids = list(scope_ids or [])

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        token = await _bearer_token(
            self._store, self._admin_id, self._instance_id, self._secret_ref
        )
        transport = self._transport
        if transport is None:
            raise ProviderUnavailable(
                "Google Drive live HTTP call is a provider-deferred seam in this arc; "
                "inject an HttpTransport (real or fake) to exercise it."
            )
        headers = {"Authorization": f"Bearer {token}"}
        # Every page of the listing (2026-09-05 audit F094): the first 100 files were
        # the whole Drive before.
        files: list[dict[str, object]] = []
        page_token: str | None = None
        for _ in range(DRIVE_MAX_PAGES):
            params: dict[str, object] = {
                "q": scope.drive_query(self._scope_ids),
                # `parents` is requested so the scope can be re-checked locally: a shared or
                # shortcut file can satisfy the query without living under a chosen folder.
                "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,trashed,parents,size)",
                "pageSize": 100,
            }
            if page_token:
                params["pageToken"] = page_token
            data = (
                await transport.get(
                    "https://www.googleapis.com/drive/v3/files", headers=headers, params=params
                )
            ).json()
            files.extend(data.get("files", []))
            page_token = data.get("nextPageToken") or None
            if not page_token:
                break
        items: list[RemoteItem] = []
        failed: list[str] = []
        for f in files:
            if f.get("trashed") or not scope.in_scope(f, self._scope_ids, parent_keys=("parents",)):
                continue
            file_id, name = str(f["id"]), str(f.get("name", f["id"]))
            try:
                text = await self._content_of(transport, headers, f)
            except (ProviderUnavailable, ValueError):
                failed.append(file_id)
                continue
            if text is None:
                continue  # a kind of file that carries no ingestible text
            items.append(
                RemoteItem(
                    external_id=file_id,
                    name=name,
                    text=text,
                    revision=str(f.get("modifiedTime", "")),
                )
            )
        return RemoteSnapshot(items=items, failed_ids=failed)

    async def _content_of(
        self, transport: HttpTransport, headers: dict[str, str], f: dict[str, object]
    ) -> str | None:
        """The ingestible text of one Drive file (2026-09-05 audit F097).

        A listing entry carries no body. Google-native documents are EXPORTED as text
        (Docs → text/plain, Sheets → text/csv, Slides → text/plain); binary files are
        downloaded and run through the same parsers an upload uses. A test fake may
        put ``text`` on the listing entry directly; that is honoured so provider
        fakes stay small. ``None`` — nothing to ingest (a folder, a form, a drawing,
        an unsupported binary, a file over the per-file cap)."""
        if isinstance(f.get("text"), str):
            return str(f["text"])
        if "mimeType" not in f:
            # Real Drive always returns mimeType (it is in the requested fields); an
            # entry without one is a minimal fake and is ingested as-is.
            return ""
        mime = str(f.get("mimeType") or "")
        file_id = str(f["id"])
        if mime.startswith("application/vnd.google-apps."):
            export_as = DRIVE_EXPORT_MIME.get(mime)
            if export_as is None:
                return None
            resp = await transport.get(
                f"https://www.googleapis.com/drive/v3/files/{file_id}/export",
                headers=headers,
                params={"mimeType": export_as},
            )
            return resp.text
        size = f.get("size")
        try:
            if size is not None and int(str(size)) > DRIVE_MAX_FILE_BYTES:
                return None
        except ValueError:
            pass
        name = str(f.get("name") or file_id)
        if parsers.format_for(name, mime) is None:
            return None
        resp = await transport.get(
            f"https://www.googleapis.com/drive/v3/files/{file_id}",
            headers=headers,
            params={"alt": "media"},
        )
        return parsers.parse_file(resp.content, filename=name, content_type=mime).text


# --------------------------------------------------------------------------- #
# Notion — `last_edited_time` filtering on connected pages/databases, §3.2.3.
# --------------------------------------------------------------------------- #
_NOTION_VERSION = "2022-06-28"

# Block types whose payload carries readable rich_text (Notion blocks API). Not
# exhaustive on purpose: tables/embeds/files have no flat text to ingest, and
# omitting them is honest emptiness rather than mangled output.
_NOTION_TEXT_BLOCKS = frozenset(
    {
        "paragraph",
        "heading_1",
        "heading_2",
        "heading_3",
        "bulleted_list_item",
        "numbered_list_item",
        "to_do",
        "quote",
        "callout",
        "toggle",
    }
)


def notion_page_title(page: dict[str, object]) -> str:
    """The page's real title, from the properties walk (audit round 3, Phase 8).

    A Notion page object has NO top-level ``title`` field: the title lives in
    ``properties`` as the one property whose type is ``"title"`` (named "title"
    on plain pages, anything on database rows), holding rich-text parts with
    ``plain_text``. The old ``page.get("title")`` read a field the API never
    sends, so every synced source was named by its UUID."""
    properties = page.get("properties")
    if isinstance(properties, dict):
        for prop in properties.values():
            if isinstance(prop, dict) and prop.get("type") == "title":
                parts = prop.get("title") or []
                text = "".join(
                    str(part.get("plain_text", "")) for part in parts if isinstance(part, dict)
                ).strip()
                if text:
                    return text
    return str(page.get("id", ""))


def notion_block_text(block: dict[str, object]) -> str:
    """One block's readable text (``rich_text`` parts of a text-bearing type)."""
    block_type = block.get("type")
    if not isinstance(block_type, str) or block_type not in _NOTION_TEXT_BLOCKS:
        return ""
    payload = block.get(block_type)
    if not isinstance(payload, dict):
        return ""
    parts = payload.get("rich_text") or []
    return "".join(
        str(part.get("plain_text", "")) for part in parts if isinstance(part, dict)
    ).strip()


class NotionProvider:
    """Enumerates connected Notion pages/databases; delta via `last_edited_time`.

    Real API shapes since Phase 8 (audit round 3): search is a POST with the
    object filter in the JSON body (the old ``GET ?filter=page`` was a shape the
    API does not accept), the title comes from the properties walk, and the page
    BODY is assembled from ``GET /v1/blocks/{page_id}/children`` — a page object
    carries no text at all, so the old ``p.get("text")`` ingested empty strings
    for every page."""

    origin = "notion"

    def __init__(
        self,
        *,
        admin_id: uuid.UUID,
        instance_id: uuid.UUID,
        store: SecretStore,
        transport: HttpTransport | None = None,
        scope_ids: list[str] | None = None,
        secret_ref: str | None = None,
    ) -> None:
        self._admin_id = admin_id
        self._instance_id = instance_id
        self._store = store
        self._transport = transport
        self._secret_ref = secret_ref
        # Selected page/database ids (Decision #9); empty = every page the grant reaches.
        self._scope_ids = list(scope_ids or [])

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "Notion-Version": _NOTION_VERSION}

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        token = await _bearer_token(
            self._store, self._admin_id, self._instance_id, self._secret_ref
        )
        transport = self._transport
        if transport is None:
            raise ProviderUnavailable(
                "Notion live HTTP call is a provider-deferred seam in this arc; inject "
                "an HttpTransport (real or fake) to exercise it."
            )
        pages: list[dict[str, object]] = []
        cursor: str | None = None
        while True:
            body: dict[str, object] = {
                "filter": {"property": "object", "value": "page"},
                "page_size": 100,
            }
            if cursor:
                body["start_cursor"] = cursor
            resp = await transport.post(
                "https://api.notion.com/v1/search",
                headers=self._headers(token),
                json=body,
            )
            data = resp.json()
            pages.extend(p for p in data.get("results", []) if isinstance(p, dict))
            cursor = data.get("next_cursor") if data.get("has_more") else None
            if not cursor:
                break
        # Notion's search endpoint has NO ancestor filter, so the scope can only be
        # enforced on the results — which is why ``in_scope`` is not merely a backstop here.
        items = []
        for p in pages:
            if p.get("archived"):
                continue
            if not scope.in_scope(p, self._scope_ids, parent_keys=("parent", "parent_id")):
                continue
            items.append(
                RemoteItem(
                    external_id=str(p["id"]),
                    name=notion_page_title(p),
                    text=await self._page_text(transport, token, str(p["id"])),
                    revision=str(p.get("last_edited_time", "")),
                )
            )
        return RemoteSnapshot(items=items)

    async def _page_text(self, transport: HttpTransport, token: str, page_id: str) -> str:
        """The page body via the blocks-children walk (paginated)."""
        lines: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, object] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            resp = await transport.get(
                f"https://api.notion.com/v1/blocks/{page_id}/children",
                headers=self._headers(token),
                params=params,
            )
            data = resp.json()
            for block in data.get("results", []):
                if isinstance(block, dict):
                    line = notion_block_text(block)
                    if line:
                        lines.append(line)
            cursor = data.get("next_cursor") if data.get("has_more") else None
            if not cursor:
                break
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CRM knowledge base — HubSpot / Salesforce, `updatedAt`/`LastModifiedDate` delta.
# --------------------------------------------------------------------------- #
#: CRM knowledge-base listing pages read per sync — a bound, not a target (F094).
CRM_KB_MAX_PAGES = 50


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
        secret_ref: str | None = None,
    ) -> None:
        self._admin_id = admin_id
        self._instance_id = instance_id
        self._store = store
        self._transport = transport
        self._secret_ref = secret_ref

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        token = await _bearer_token(
            self._store, self._admin_id, self._instance_id, self._secret_ref
        )
        transport = self._transport
        if transport is None:
            raise ProviderUnavailable(
                "HubSpot live HTTP call is a provider-deferred seam in this arc; inject "
                "an HttpTransport (real or fake) to exercise it."
            )
        items: list[RemoteItem] = []
        after: str | None = None
        for _ in range(CRM_KB_MAX_PAGES):  # every page (F094), bounded
            params: dict[str, object] = {"archived": "false", "limit": 100}
            if after:
                params["after"] = after
            data = (
                await transport.get(
                    "https://api.hubapi.com/cms/v3/knowledge-base/articles",
                    headers={"Authorization": f"Bearer {token}"},
                    params=params,
                )
            ).json()
            items.extend(
                RemoteItem(
                    external_id=str(a["id"]),
                    name=a.get("title", str(a["id"])),
                    text=a.get("postBody", a.get("text", "")),
                    revision=a.get("updatedAt", ""),
                )
                for a in data.get("results", [])
            )
            after = ((data.get("paging") or {}).get("next") or {}).get("after") or None
            if not after:
                break
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
        instance_url: str | None = None,
        transport: HttpTransport | None = None,
        secret_ref: str | None = None,
    ) -> None:
        self._admin_id = admin_id
        self._instance_id = instance_id
        self._store = store
        self._instance_url = instance_url
        self._transport = transport
        self._secret_ref = secret_ref

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        material = await _token_material(
            self._store, self._admin_id, self._instance_id, self._secret_ref
        )
        token = material["access_token"]
        # Salesforce is the one provider with no fixed API host: the token exchange
        # returns the org's own ``instance_url``, and login.salesforce.com serves the
        # login flow only — querying it would 404 for every real customer. Read from
        # the stored material rather than a constructor default, which is the only
        # place the per-org value exists (P1-2).
        base = self._instance_url or material.get("instance_url") or "https://login.salesforce.com"
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
        headers = {"Authorization": f"Bearer {token}"}
        data = (
            await transport.get(
                f"{base}/services/data/v58.0/query", headers=headers, params={"q": query}
            )
        ).json()
        items: list[RemoteItem] = []
        for _ in range(CRM_KB_MAX_PAGES):  # every page (F094): SOQL answers 2000 at a time
            items.extend(
                RemoteItem(
                    external_id=str(r["Id"]),
                    name=r.get("Title", str(r["Id"])),
                    text=r.get("Summary", r.get("text", "")),
                    revision=r.get("LastModifiedDate", ""),
                )
                for r in data.get("records", [])
            )
            next_url = data.get("nextRecordsUrl")
            if data.get("done", True) or not next_url:
                break
            data = (await transport.get(f"{base}{next_url}", headers=headers)).json()
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


CRAWL_USER_AGENT = "LucielBot"
#: Redirect hops one page fetch may follow (round 7 WP-4c #1). An apex→www 301 is the
#: normal shape of a small business site; five covers apex→www→https→trailing-slash
#: with room to spare, and stops a redirect loop from spinning.
CRAWL_MAX_REDIRECTS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_HREF_RE = re.compile(r"""<a\s[^>]*?href\s*=\s*["']([^"'#]+)""", re.IGNORECASE)


class CrawlFetchRefused(ProviderUnavailable):
    """One page answered with a non-success status (carries it, so robots.txt can tell
    a 401/403 — "keep out" under RFC 9309 — from a 404/5xx — "no rule")."""

    def __init__(self, url: str, status_code: int) -> None:
        super().__init__(f"{urlparse(url).hostname} answered {status_code} for {url}")
        self.status_code = status_code


class CrawlUnreachable(ProviderUnavailable):
    """A whole crawl reached no page (every fetch timed out, 403'd, 5xx'd or was
    refused) — the SITE is unreachable this pass, not a credential (round 7 Phase 2
    review, knowledge #2). A crawl connection holds no sign-in, so this must never
    take the revocation path (D10 stop clock, paused rows, "your sign-in was revoked"
    email, the connection row flipped to error): the sweep and the manual routes
    read it as an ordinary failed sync. A subclass, so every caller that only knows
    ``ProviderUnavailable`` (and the manual routes' 409 wording) is unchanged."""


_SKIP_EXTENSIONS = (
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".webp",
    ".zip",
    ".mp4",
    ".mp3",
    ".css",
    ".js",
    ".ico",
    ".woff",
    ".woff2",
    ".xml",
    ".json",
)


def _normalize_url(url: str) -> str:
    parts = urlparse(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    path = parts.path or "/"
    return urlunparse((parts.scheme, parts.netloc.lower(), path, "", parts.query, ""))


def _same_origin_links(page_url: str, html: str) -> list[str]:
    """Links on the page that stay on the page's scheme+host, deduped, in order.
    Assets and downloads are skipped — this is a text crawl."""
    base = urlparse(page_url)
    out: list[str] = []
    seen: set[str] = set()
    for raw in _HREF_RE.findall(html):
        candidate = raw.strip()
        if candidate.lower().startswith(("mailto:", "tel:", "javascript:")):
            continue
        absolute = _normalize_url(urljoin(page_url, candidate))
        if not absolute:
            continue
        parsed = urlparse(absolute)
        if (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc.lower()):
            continue
        if parsed.path.lower().endswith(_SKIP_EXTENSIONS):
            continue
        if absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


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
        httpx_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._urls = urls
        self._transport = transport
        # The wire-level transport the REAL fetch path builds its client on (tests bind
        # an ``httpx.MockTransport`` here so the pin / redirect / header behaviour is
        # exercised exactly as production runs it, with no socket). ``None`` = httpx's
        # default network transport.
        self._httpx_transport = httpx_transport
        settings = get_settings()
        self._timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else settings.website_crawl_timeout_seconds
        )
        self._max_bytes = max_bytes if max_bytes is not None else settings.website_crawl_max_bytes

    async def _fetch_one(self, url: str) -> str:
        body, _final_url = await self._fetch_page(url)
        return body

    async def _fetch_page(self, url: str) -> tuple[str, str]:
        """Fetch one page; returns ``(body, final_url)`` — the URL the body actually
        came from, after any redirects (so links on it resolve against the right base)."""
        if self._transport is not None:
            # An injected transport is a seam trusted code chose, not a socket this
            # process opens against a tenant-supplied host.
            resp = await self._transport.get(url)
            body = resp.text
            final_url = url
        else:
            body, final_url = await self._fetch_real(url)
        if len(body.encode("utf-8")) > self._max_bytes:
            body = body.encode("utf-8")[: self._max_bytes].decode("utf-8", errors="ignore")
        return body, final_url

    async def _fetch_real(self, url: str) -> tuple[str, str]:
        # SSRF gate immediately before the real connect, and re-derived on every
        # sync rather than trusted from create time: the DNS answer for a host the
        # admin registered last month is not the one it gives today. The crawled
        # body is readable back through GET /sources/{id}/chunks, so an unguarded
        # fetch is an exfiltration channel, not a blind SSRF (P1-4).
        # ...and CONNECT to the address that was validated (adversarial review
        # R-WP4-1 — the F090 pin the webhook POST already had). A DNS answer that
        # flipped between the check and the connect (rebinding) reached the task
        # metadata endpoint with a clean bill of health, and what it returned
        # became readable chunks. The name still travels as Host + SNI, so TLS
        # verifies the certificate against the hostname the owner registered.
        #
        # Redirects are followed BY HAND (round 7 WP-4c #1): httpx's own follower
        # would connect to whatever ``Location`` names without the pin, so every hop
        # is re-resolved and re-validated exactly like the first URL. Before this the
        # client followed nothing and ``raise_for_status`` read an apex→www 301 as a
        # failed page — a whole crawl of a site that redirects its apex fetched no
        # pages at all. The bot identifies itself on every request (robots.txt rules
        # for "LucielBot" were honoured while the fetch itself was anonymous).
        current = url
        client_kwargs: dict[str, object] = {"timeout": self._timeout}
        if self._httpx_transport is not None:
            client_kwargs["transport"] = self._httpx_transport
        async with httpx.AsyncClient(**client_kwargs) as client:  # type: ignore[arg-type]
            for _hop in range(CRAWL_MAX_REDIRECTS + 1):
                try:
                    pinned_url, host = sandbox.pin_public_target(current)
                except sandbox.SsrfBlocked as exc:
                    raise ProviderUnavailable(
                        f"crawl URL is not a public http(s) address: {current}"
                    ) from exc
                resp = await client.get(
                    pinned_url,
                    headers={"Host": host, "User-Agent": CRAWL_USER_AGENT},
                    extensions={"sni_hostname": host},
                    follow_redirects=False,
                )
                location = resp.headers.get("location")
                if resp.status_code in _REDIRECT_STATUSES and location:
                    current = urljoin(current, location)
                    if urlparse(current).scheme not in ("http", "https"):
                        raise ProviderUnavailable(f"crawl redirect left http(s): {current}")
                    continue
                if resp.status_code >= 400:
                    raise CrawlFetchRefused(current, resp.status_code)
                resp.raise_for_status()
                return resp.text, current
        raise ProviderUnavailable(
            f"crawl URL redirected more than {CRAWL_MAX_REDIRECTS} times: {url}"
        )

    async def snapshot(self, connection_id: uuid.UUID) -> RemoteSnapshot:
        """Fetch every seed URL and the same-origin pages it links to, breadth-first,
        bounded by ``website_crawl_max_pages`` / ``website_crawl_max_depth`` and
        ``robots.txt`` (2026-09-05 audit F117/F123). A page that fails to fetch is
        reported in ``failed_ids`` so the sync keeps what it knew, never retires it."""
        import hashlib

        settings = get_settings()
        max_pages = max(1, settings.website_crawl_max_pages)
        max_depth = max(0, settings.website_crawl_max_depth)
        items: list[RemoteItem] = []
        failed: list[str] = []
        robots: dict[str, RobotFileParser | None] = {}
        queue: list[tuple[str, int]] = [(u, 0) for u in self._urls]
        seen: set[str] = set()
        fetched_any = False
        while queue and len(items) + len(failed) < max_pages:
            url, depth = queue.pop(0)
            url = _normalize_url(url)
            if not url or url in seen:
                continue
            seen.add(url)
            if not await self._robots_allow(url, robots):
                continue
            try:
                html, final_url = await self._fetch_page(url)
            except (ProviderUnavailable, httpx.HTTPError, ValueError):
                failed.append(url)
                continue
            text = _extract_visible_text(html)
            revision = hashlib.sha256(text.encode("utf-8")).hexdigest()
            items.append(RemoteItem(external_id=url, name=url, text=text, revision=revision))
            fetched_any = True
            if depth < max_depth:
                # Links resolve against the page the body CAME from: after an apex→www
                # redirect the site's relative links belong to www, and following them
                # from the apex base would redirect every one of them again.
                for link in _same_origin_links(final_url, html):
                    if link not in seen:
                        queue.append((link, depth + 1))
        if not fetched_any and failed:
            # Every page failed (a blocked address, a dead site): raise rather than
            # hand the sync an 'empty site' it would read as a mass retirement.
            raise CrawlUnreachable(f"crawl fetched no pages: {failed[0]}")
        return RemoteSnapshot(items=items, failed_ids=failed)

    async def _robots_allow(self, url: str, cache: dict[str, RobotFileParser | None]) -> bool:
        """robots.txt per origin, fetched once per snapshot; unreachable robots = allowed
        (the site made no rule), a fetched one is honoured for our user agent, and a
        401/403 on robots.txt is "keep out" for the whole origin (RFC 9309 §2.3.1.3 —
        round 7 WP-4c #1; it used to read as "no rule")."""
        origin = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
        if origin not in cache:
            parser: RobotFileParser | None = None
            try:
                body = await self._fetch_one(f"{origin}/robots.txt")
                parser = RobotFileParser()
                parser.parse(body.splitlines())
            except CrawlFetchRefused as exc:
                if exc.status_code in (401, 403):
                    parser = RobotFileParser()
                    parser.parse(["User-agent: *", "Disallow: /"])
                else:
                    parser = None
            except (ProviderUnavailable, httpx.HTTPError, ValueError):
                parser = None
            cache[origin] = parser
        parser = cache[origin]
        return True if parser is None else parser.can_fetch(CRAWL_USER_AGENT, url)


# --------------------------------------------------------------------------- #
# Scope CANDIDATES (Decision #9): what the owner may choose from.
# --------------------------------------------------------------------------- #
async def list_scope_candidates(
    connection: object, store: SecretStore, transport: HttpTransport
) -> list[dict[str, str]]:
    """The folders (Drive) or pages/databases (Notion) the owner may narrow the scope to.

    Read-only and non-secret: returns ids and display names only, so the choice can be
    rendered without the UI ever holding credential material (§3.8.3). Raises
    ``ProviderUnavailable`` for providers that have nothing to narrow — an owner should get a
    clean 422, not an empty list that looks like "your Drive has no folders"."""
    provider_name = connection.provider  # type: ignore[attr-defined]
    admin_id = connection.admin_id  # type: ignore[attr-defined]
    instance_id = connection.instance_id  # type: ignore[attr-defined]
    token = await _bearer_token(
        store, admin_id, instance_id, getattr(connection, "secret_ref", None) or None
    )
    if provider_name == "google_drive":
        resp = await transport.get(
            "https://www.googleapis.com/drive/v3/files",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "q": "mimeType='application/vnd.google-apps.folder' and trashed=false",
                "fields": "files(id,name)",
            },
        )
        return [
            {"id": str(f["id"]), "name": str(f.get("name", f["id"])), "kind": "folder"}
            for f in resp.json().get("files", [])
        ]
    if provider_name == "notion":
        # Search is a POST (real API shape, Phase 8); no object filter here — the
        # scope picker offers pages AND databases. Names via the properties walk
        # for pages; databases carry a top-level rich-text ``title`` list.
        resp = await transport.post(
            "https://api.notion.com/v1/search",
            headers={"Authorization": f"Bearer {token}", "Notion-Version": _NOTION_VERSION},
            json={"page_size": 100},
        )

        def _candidate_name(p: dict[str, object]) -> str:
            if p.get("object") == "database":
                parts = p.get("title")
                if not isinstance(parts, list):
                    parts = []
                text = "".join(
                    str(part.get("plain_text", "")) for part in parts if isinstance(part, dict)
                ).strip()
                if text:
                    return text
                return str(p.get("id", ""))
            return notion_page_title(p)

        return [
            {
                "id": str(p["id"]),
                "name": _candidate_name(p),
                "kind": "database" if p.get("object") == "database" else "page",
            }
            for p in resp.json().get("results", [])
            if not p.get("archived")
        ]
    raise ProviderUnavailable(f"'{provider_name}' has no selectable scope.")


# --------------------------------------------------------------------------- #
# Dispatch: build the real provider for an instance_connections row (§3.2.3/§3.8).
# --------------------------------------------------------------------------- #
def provider_for_connection(connection: object, store: SecretStore) -> SyncProvider:
    """Return the concrete ``SyncProvider`` for a knowledge_source connection row.

    ``connection`` is an ``InstanceConnection``; typed as ``object`` here to avoid
    importing the model into this module's public type signature (keeps this
    module import-light for tests that only need the provider classes). The
    caller (admin API) always passes a real ``InstanceConnection``.
    """
    provider_name = connection.provider  # type: ignore[attr-defined]
    admin_id = connection.admin_id  # type: ignore[attr-defined]
    instance_id = connection.instance_id  # type: ignore[attr-defined]
    # The owner's chosen scope is read HERE, off the live connection row, so every sync
    # path (first sync, manual re-sync, scheduled re-sync) honours it without each caller
    # having to remember to pass it (Decision #9).
    selected = scope.scope_ids(connection)
    # None outside live mode, which is what every provider's "inject an HttpTransport"
    # guard already reads — so mock mode is unchanged and the fakes tests inject still
    # win. In live mode this is the binding whose absence made every OAuth-class sync
    # report itself as a deferred seam (P1-2).
    transport = live_http_transport()
    # The row's OWN secret ref (F092): several knowledge connections coexist, each
    # with its own token. None for legacy rows → the shared-ref fallback.
    secret_ref = getattr(connection, "secret_ref", None) or None
    if provider_name == "google_drive":
        return GoogleDriveProvider(
            admin_id=admin_id,
            instance_id=instance_id,
            store=store,
            scope_ids=selected,
            transport=transport,
            secret_ref=secret_ref,
        )
    if provider_name == "notion":
        return NotionProvider(
            admin_id=admin_id,
            instance_id=instance_id,
            store=store,
            scope_ids=selected,
            transport=transport,
            secret_ref=secret_ref,
        )
    if provider_name == "hubspot":
        return HubSpotProvider(
            admin_id=admin_id,
            instance_id=instance_id,
            store=store,
            transport=transport,
            secret_ref=secret_ref,
        )
    if provider_name == "salesforce":
        return SalesforceProvider(
            admin_id=admin_id,
            instance_id=instance_id,
            store=store,
            transport=transport,
            secret_ref=secret_ref,
        )
    if provider_name == "website_crawl":
        cfg = connection.non_secret_config or {}  # type: ignore[attr-defined]
        urls = cfg.get("crawl_urls") or []
        return WebsiteCrawlProvider(urls=list(urls))
    raise ProviderUnavailable(f"No sync provider registered for '{provider_name}'.")
