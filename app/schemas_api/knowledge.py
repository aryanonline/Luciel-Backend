"""Knowledge wire shapes — mirror schemas/luciel.ts (KnowledgeSource/Chunk/Quota)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field, HttpUrl

from app.schemas_api.common import WireModel

KnowledgeSourceOriginWire = Literal[
    "upload", "paste", "csv", "website_crawl", "google_drive", "notion", "crm_kb"
]
IngestionStatusWire = Literal["pending", "ready", "error"]
# Architecture §3.2.3 sync_status vocabulary. paused_reconnect_needed replaces the
# former needs_reconnect (Axis4-F02); error surfaces non-revocation sync failures.
KnowledgeSyncStatusWire = Literal["synced", "syncing", "paused_reconnect_needed", "error"]


class KnowledgeSourceOut(WireModel):
    source_id: uuid.UUID
    name: str
    origin: KnowledgeSourceOriginWire
    ingestion_status: IngestionStatusWire
    size_bytes: int
    last_updated_at: datetime
    last_synced_at: datetime | None = None
    sync_status: KnowledgeSyncStatusWire | None = None
    # §3.2.2 delete-confirmation truth (C14): how many answers in the last 7
    # days drew on this source, read from the durable retrieval trace (C6).
    # 0 is a real zero once the store exists — the trust-contract tooltip
    # ("N questions used this source") reads this before a delete.
    # Explicit alias: the generated camelization renders `_7d` as `7D`, which
    # silently DROPPED the serializer's `usedByQuestions7d` input (field stayed
    # 0 on the wire) and never matched the frontend schema's lowercase key —
    # live-caught on the 2026-08-23 dev walkthrough (the modal claimed "no
    # questions used this source" about a source analytics counted 6 for).
    used_by_questions_7d: int = Field(0, alias="usedByQuestions7d")


class KnowledgeChunkOut(WireModel):
    chunk_id: uuid.UUID
    source_id: uuid.UUID
    ordinal: int
    text: str


class KnowledgeQuotaOut(WireModel):
    used_bytes: int
    total_bytes: int
    per_file_max_bytes: int


class PasteIngestRequest(WireModel):
    name: str
    text: str


class RenameSourceRequest(WireModel):
    """PUT /sources/{id} body — the rename affordance (Architecture §3.2.2)."""

    name: str


# --- Live-sync connectors (§3.2.3) ------------------------------------------- #
# Provider is the concrete BRAND (mirrors app.connections.service: connection_type
# is generic, provider is the brand) — google_drive/notion/website_crawl map 1:1
# to a source_origin, while hubspot/salesforce both map to source_origin='crm_kb'
# (§3.2.3 storage entity note: "CRM's KB articles" is one origin, two native
# connectors, exactly like the Vision §7 "native CRM connectors (HubSpot /
# Salesforce)" line item).
KnowledgeSyncProviderWire = Literal[
    "google_drive", "notion", "hubspot", "salesforce", "website_crawl"
]


class CreateSyncConnectionRequest(WireModel):
    """POST /sync-connections body: create a knowledge-sync connection (§3.2.3).

    Reuses the Connections layer (§3.8) — this is the knowledge-specific create
    call that wraps ``connections.start_connection`` with connection_type fixed to
    'knowledge_source'. ``crawl_urls`` is required (and only meaningful) for
    provider='website_crawl', which has no OAuth step (§3.2.3 crawl-sync note).
    """

    provider: KnowledgeSyncProviderWire
    # HttpUrl, not str: the crawler fetches these and the body is readable back through
    # the chunks endpoint, so ``file:///etc/passwd`` must fail at the boundary with a 422
    # rather than reach the fetch path at all (P1-4). The runtime SSRF gate in
    # ``WebsiteCrawlProvider`` is the other half — this one cannot check DNS, and DNS is
    # what changes between registration and the next sync.
    crawl_urls: list[HttpUrl] | None = None


class SyncConnectionOut(WireModel):
    """Non-secret view of a knowledge-sync connection (§3.8.3 secret hygiene)."""

    connection_id: uuid.UUID
    provider: str
    status: str
    non_secret_config: dict[str, object] | None = None
    status_detail: str | None = None


class SyncResultOut(WireModel):
    """Result of a manual sync/crawl trigger: external_ids added/updated/removed."""

    added: list[str]
    updated: list[str]
    removed: list[str]


# --- Knowledge SCOPE selection (Decision #9) --------------------------------- #
# "Connect Google Drive" used to mean "read everything this token can reach". These
# shapes let the owner pick the folders (Drive) or pages/databases (Notion) Luciel may
# read. All non-secret: ids and display names only (§3.8.3).


class ScopeCandidateOut(WireModel):
    """One folder/page/database the owner may select."""

    id: str
    name: str
    # 'folder' (Drive) | 'page' | 'database' (Notion).
    kind: str


class ScopeSelectionIn(WireModel):
    """One selected folder/page id. ``name`` is stored for display only."""

    id: str
    name: str | None = None


class SaveScopeRequest(WireModel):
    """PUT /sync-connections/{id}/scope body.

    An EMPTY ``selections`` list clears the scope, which means the WHOLE connected
    account — the pre-Decision-#9 behavior — not "nothing"."""

    selections: list[ScopeSelectionIn]


class KnowledgeScopeOut(WireModel):
    """The scope currently in force for one knowledge-sync connection."""

    connection_id: uuid.UUID
    provider: str
    # 'drive_folders' | 'notion_pages' | null when the provider has no selectable scope.
    scope_kind: str | None = None
    selections: list[ScopeCandidateOut]
    # True when nothing is selected: Luciel reads the whole connected account.
    whole_account: bool


class OAuthCallbackRequest(WireModel):
    """POST /sync-connections/{id}/oauth-callback body (§3.8.3/§3.2.3).

    The provider consent-screen redirect lands the admin's browser back on the
    frontend with ``code`` (and the ``state`` it round-tripped, for CSRF binding
    to the connection being completed); the frontend forwards both here so the
    backend — never the browser — holds the client_secret needed to exchange
    the code for a token.
    """

    code: str
    state: str | None = None
