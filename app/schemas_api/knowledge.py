"""Knowledge wire shapes — mirror schemas/luciel.ts (KnowledgeSource/Chunk/Quota)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, HttpUrl

from app.schemas_api.common import WireModel

KnowledgeSourceOriginWire = Literal[
    "upload", "paste", "csv", "website_crawl", "google_drive", "notion", "crm_kb"
]
IngestionStatusWire = Literal["pending", "ready", "error"]
# Architecture §3.2.3 sync_status vocabulary. paused_reconnect_needed replaces the
# former needs_reconnect (Axis4-F02); error surfaces non-revocation sync failures.
KnowledgeSyncStatusWire = Literal["synced", "syncing", "paused_reconnect_needed", "error"]
# ``knowledge_sources.deleted_by`` (migration 0056; knowledge.service.DELETED_BY_*).
KnowledgeDeletedByWire = Literal["owner", "origin", "lifecycle"]


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
    # The sync connection a live source belongs to (2026-09-05 audit: the pillar
    # confirms a refused shrink against the CONNECTION, not the source).
    connection_id: uuid.UUID | None = None
    # Round 7 M10 (owner decision D10): when a revoked/expired grant paused this
    # source (``knowledge_sources.paused_since``). It keeps answering for
    # ``service.PAUSED_SERVING_DAYS`` from this instant, then stops until a clean
    # sync — the dashboard says "stops answering on <date>" from it. Null = not paused.
    paused_since: datetime | None = None
    # Who tombstoned the source (``knowledge_sources.deleted_by``): ``owner`` (undo-able
    # for 30 days), ``origin`` (gone at Drive / Notion / the site — never restorable
    # here), ``lifecycle`` (comes back with the Luciel). Null on a live source.
    deleted_by: KnowledgeDeletedByWire | None = None


class KnowledgeChunkOut(WireModel):
    chunk_id: uuid.UUID
    source_id: uuid.UUID
    ordinal: int
    text: str


class KnowledgeChunkPageOut(WireModel):
    """GET /sources/{id}/chunks — the first page of a source's chunks WITH the count.

    Round 7 WP-4c #6: the route returned the default page (20 rows) and nothing else,
    so the raw view printed "first N of <page length>" — a source with 300 chunks read
    "first 20 of 20". ``total_chunks`` is the real count; ``page_size`` is the page the
    ``chunks`` list is bounded by (the route does not page further yet)."""

    chunks: list[KnowledgeChunkOut]
    total_chunks: int
    page_size: int


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
    # The last manual or scheduled sync of this source (round 7 walk run 8): what the
    # dashboard reads after a POST .../sync answered 202, and on every later visit.
    # ``None`` when the source never ran a sync job.
    sync_job: SyncJobOut | None = None


class SyncResultOut(WireModel):
    """Result of a manual sync/crawl trigger: external_ids added/updated/removed, and
    the ones the storage quota refused (round 7 Phase 2 review, knowledge #4) — a new
    item over the per-file cap or the tenant total is not ingested, a changed one keeps
    its previous revision; either way the owner is told, never a silently absent page."""

    added: list[str]
    updated: list[str]
    removed: list[str]
    refused: list[str] = []


#: ``SyncJobOut.state`` vocabulary. ``syncing`` — the job is running (a "syncing" with no
#: job behind it is served as ``failed``, interrupted); ``synced`` — it
#: finished and ``result`` says what changed; ``failed`` — it ended without changing
#: anything and ``error`` says why in the owner's words; ``shrink_pending`` — the
#: source reported far fewer items than are synced, nothing was removed, and ``shrink``
#: carries the counts the owner confirms with ``?confirmShrink=true``.
SyncJobState = Literal["syncing", "synced", "failed", "shrink_pending"]


class SyncJobShrinkOut(WireModel):
    remote: int
    existing: int


class SyncJobOut(WireModel):
    """The knowledge sync JOB on a connection (``instance_connections.sync_job``).

    A manual sync answers 200 ``SyncResultOut`` when it finishes within the inline
    wait, and 202 ``SyncJobAcceptedOut`` when it is still running; either way this
    record on the connection is the durable answer."""

    state: SyncJobState
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: SyncResultOut | None = None
    error: str | None = None
    shrink: SyncJobShrinkOut | None = None
    # 'manual' (the owner's Sync now) | 'scheduled' (the knowledge sweep).
    trigger: Literal["manual", "scheduled"] | None = None

    @classmethod
    def from_record(cls, job: dict[str, Any] | None) -> SyncJobOut | None:
        """The wire view of an ``instance_connections.sync_job`` record already read
        through ``app.knowledge.sync_job.effective_job`` (liveness). ``result``
        carries the wire's four lists; the row keeps the rest of the pass."""
        if not job or not job.get("state"):
            return None
        result = job.get("result")
        return cls.model_validate(
            {
                "state": job["state"],
                "startedAt": job.get("started_at"),
                "finishedAt": job.get("finished_at"),
                "result": (
                    {k: result.get(k) or [] for k in ("added", "updated", "removed", "refused")}
                    if isinstance(result, dict)
                    else None
                ),
                "error": job.get("error"),
                "shrink": job.get("shrink"),
                "trigger": job.get("trigger"),
            }
        )


class SyncJobAcceptedOut(SyncJobOut):
    """The 202 body of POST /sync-connections/{id}/sync: the job is running and the
    connection's ``syncJob`` will carry its outcome. Never a success claim."""

    connection_id: uuid.UUID


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


SyncConnectionOut.model_rebuild()
