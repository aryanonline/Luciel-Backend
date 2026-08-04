"""Sessions, transcripts, session summaries, escalation events (Architecture §3.4/§3.5)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin


class Session(Base, TenantMixin, TimestampMixin):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    resolved_lead_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="ai")
    counted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # §3.7.1/§3.8.7 rule E: the EFFECTIVE additive/neutral config (enabled tools,
    # personality, enabled channels) frozen at session-open. Additive pillar changes
    # made mid-conversation apply to NEW sessions only — this snapshot is the capability
    # set the orchestrator reads from. Usability of each capability stays LIVE via the
    # broker's connection/health gates (safety half wins). NULL → fall back to live config.
    config_snapshot: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)


class Transcript(Base, TenantMixin, TimestampMixin):
    __tablename__ = "transcripts"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Harmony fix 2026-08-04 (item 3): the real per-turn grounding score computed by
    # app/runtime/grounding.py::score_grounding was computed and used for the
    # escalation floor gate but never persisted anywhere, so the dashboard evidence
    # endpoint (GET .../evidence) had nothing real to read and returned a hardcoded
    # 0.50 for every message regardless of what actually happened. NULL for
    # non-luciel roles (lead/human_agent turns are not scored).
    grounding_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Harmony wave 2, item 2a (LIVE-VERIFIED): real retriever chunk references
    # (source_id/source_name/text), persisted at reply time for luciel turns whose
    # retriever actually returned chunks. NULL means attribution was never captured
    # for this row (legacy rows, or a channel/role that never retrieves) -- it is
    # NOT evidence that no source backed the answer. An empty list ([]) means
    # retrieval genuinely ran and found nothing. The dashboard evidence endpoint
    # (app/api/v1/dashboard.py::get_answer_evidence) maps NULL vs [] vs non-empty to
    # three distinct attributionStatus values -- never collapses NULL and [] into
    # the same "no source backed this answer" claim.
    source_chunks: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)


class SessionSummary(Base, TenantMixin, TimestampMixin):
    __tablename__ = "session_summaries"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    resolved_lead_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    session_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)


class EscalationEvent(Base, TenantMixin, TimestampMixin):
    __tablename__ = "escalation_events"

    escalation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    session_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    resolved_lead_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    signal: Mapped[str] = mapped_column(String(32), nullable=False)
    gate: Mapped[str] = mapped_column(String(16), nullable=False)
    score_or_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    fired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
