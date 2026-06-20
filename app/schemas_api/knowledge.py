"""Knowledge wire shapes — mirror schemas/luciel.ts (KnowledgeSource/Chunk/Quota)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

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
