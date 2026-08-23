"""Durable trace store — tool dispatches + retrieval attribution (§5.4, audit round 3 C6).

Two small, PII-light fact tables close three doc promises at once:

  * ``tool_trace_events`` — one row per broker dispatch with the five-outcome
    class (§3.3.4 rule 3: every outcome is traced — DURABLY, not the broker's
    old in-memory list that died with the request).
  * ``retrieval_trace_events`` — one row per (answer turn, knowledge source)
    the runtime actually retrieved from. Feeds the §3.9 top-knowledge-sources
    analytics and the §3.2.2 delete-confirmation tooltip ("N questions in the
    last 7 days used this source") — both of which served an honest
    "not tracked yet" gap until this table existed.

No lead PII in either: tool traces carry outcome metadata (missing field NAMES,
non-secret reasons — never input values); retrieval traces carry source ids.
Both age out with the 1-year session-content retention (§3.4.10).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin


class ToolTraceEvent(Base, TenantMixin, TimestampMixin):
    """One broker dispatch outcome (§3.3.4 rule 3)."""

    __tablename__ = "tool_trace_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # Nullable: dispatches outside a conversation (none today) stay recordable.
    session_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    tool_id: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    connection_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Field NAMES only (§3.3.7) — never the values the customer supplied.
    missing_fields: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    # Non-secret reason string on failed (the contract already guarantees this).
    reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RetrievalTraceEvent(Base, TenantMixin, TimestampMixin):
    """One (answer turn, knowledge source) retrieval fact (§3.9 / §3.2.2)."""

    __tablename__ = "retrieval_trace_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # The luciel transcript row this retrieval backed, when persisted in-turn.
    transcript_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    source_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
