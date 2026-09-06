"""Knowledge sources + chunks (Architecture §3.2). Embedding column added in Arc 11."""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin

EMBED_DIM = 1536


class KnowledgeSource(Base, TenantMixin, TimestampMixin):
    __tablename__ = "knowledge_sources"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    ingestion_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    source_origin: Mapped[str | None] = mapped_column(String(32), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    connection_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    soft_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Who tombstoned the row (migration 0056): "owner" (undo-able for 30 days, then
    # purged by the retention pass) or "lifecycle" (the Luciel delete; comes back with
    # the Luciel). Restore paths read this so a Luciel restore never revives a source
    # the owner had deleted on purpose (audit F024/F109).
    deleted_by: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # ``original_object_key`` is the SYNC revision marker (knowledge/sync.py), not an
    # object key. The uploaded file itself lives under ``original_key`` in the
    # originals store (migration 0056, audit F016) so the account export can return it.
    original_object_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    original_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    original_content_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class KnowledgeChunk(Base, TenantMixin, TimestampMixin):
    __tablename__ = "knowledge_chunks"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM), nullable=True)
    # Which embedding model produced ``embedding`` (2026-09-05 audit F121): the
    # knowledge_sync sweep re-embeds rows whose model is not the current pin.
    embedding_model: Mapped[str | None] = mapped_column(String(64), nullable=True)


# --- Graph store (Architecture §3.2.1 — Path Locked: PostgreSQL ONLY) ------------- #
# The graph store is a DOMAIN-AGNOSTIC typed-node + directed-typed-edge model (§3.2.1):
# entity_type/relationship_type are free-text strings INFERRED at ingest from content +
# the admin's business description — the platform imposes NO vertical-specific ontology
# (§3.2.1 table is illustrative, not enumerated). Both tables are TenantMixin so the RLS
# generator (app/db/rls.py) + the isolation sweep auto-cover them, and both carry source_id
# so they cascade-delete with their KnowledgeSource exactly like KnowledgeChunk does (§3.2.1
# rule 5 / §3.2.2 soft-delete cascade). Stored entirely inside the existing Postgres footprint
# — NO Neo4j/Memgraph/new vendor (§3.2.1 Path-Lock, Locked Decision §7 #4).


class KnowledgeEntity(Base, TenantMixin, TimestampMixin):
    __tablename__ = "knowledge_entities"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # entity_type is inferred, not from a fixed ontology (§3.2.1 domain-agnostic).
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # canonical (normalized) name is the traversal/membership key; name is the surface form.
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    canonical: Mapped[str] = mapped_column(String(512), nullable=False)
    attributes: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)


class KnowledgeEdge(Base, TenantMixin, TimestampMixin):
    __tablename__ = "knowledge_edges"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    src_entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    dst_entity_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # relationship_type is inferred from source context (§3.2.1 domain-agnostic).
    relationship_type: Mapped[str] = mapped_column(String(64), nullable=False)
    attributes: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
