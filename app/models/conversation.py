"""Sessions, transcripts, session summaries, escalation events (Architecture §3.4/§3.5)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func
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
    # §3.4.8 session key: tenant-salted sha256(admin_id ‖ channel ‖ normalized transport
    # identifier). NULL on the widget (weak identity — resumes only by session_id) and on
    # pre-split legacy rows. This is what lets an SMS thread's second text land in the
    # SAME billed session instead of opening (and billing) a new one per message.
    participant_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="ai")
    # DEPRECATED: never written True and never read. The fire-once billing guard is
    # the counted_sessions ledger (app/models/budget.py), and every conversation
    # count — budget, analytics, the Today card, the morning brief — joins that
    # ledger (round 7 WP-3a: the employee status read this column and so reported
    # "0 conversations" forever). Do not seed it True in tests; count a session
    # through app.runtime.budget.count_session_once. Kept only for schema stability.
    counted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_message_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Explicit session END (§3.4.8): stamped by the finalization sweep at
    # last_message_at + channel timeout, or by an explicit end signal — the widget
    # close beacon (POST /sessions/{id}/end) or the Twilio voice status callback
    # (POST /webhooks/twilio/voice/status, fired at hangup). NULL = still live (or
    # not yet finalized). An ended session is never resumed, even inside the window.
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Hot→cold transcript offload markers (Commitment #38, migration 0036). Session-
    # level because "moved" deletes the hot Transcript rows once the S3 object is
    # durably written — a per-row marker would be deleted with the rows it marks.
    transcripts_archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    archive_object_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # §3.7.1/§3.8.7 rule E: the EFFECTIVE additive/neutral config (enabled tools,
    # personality, enabled channels) frozen at session-open. Additive pillar changes
    # made mid-conversation apply to NEW sessions only — this snapshot is the capability
    # set the orchestrator reads from. Usability of each capability stays LIVE via the
    # broker's connection/health gates (safety half wins). NULL → fall back to live config.
    config_snapshot: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    # Round 6 WP-H (F060, migration 0063): an answer the customer asked to receive on
    # another channel, parked until they confirm or supply where it goes
    # (app/runtime/channel_switch.PendingOutbound). NULL = nothing pending.
    pending_outbound: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    # §3.3.7 ask-bound (audit round 3, migration 0046): consecutive turns in this
    # session that stalled on a broker missing_input/ambiguous rejection with nothing
    # resolving. Reset on any `ok` tool outcome; at ASK_ATTEMPT_BOUND the orchestrator
    # stops re-asking and hands off with the partial context (§3.3.7 rule 4).
    input_ask_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )


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
    # the same "no source backed this answer" claim. A one-entry list carrying only
    # {"unavailable": reason} (app.runtime.knowledge_retrieval) means retrieval
    # never ran for the turn (embedding provider failed, 2026-09-06 E2E walk) and
    # maps to a fourth status, retrieval_unavailable.
    source_chunks: Mapped[list[dict[str, object]] | None] = mapped_column(JSONB, nullable=True)
    # #5c (live-caught 2026-08-23, migration 0051): True for luciel rows composed in
    # CODE, not by the model — the persisted greeting, the at-cap graceful reply,
    # escalation acks, the LLM-outage ack. Scoring does not APPLY to these (there is
    # no knowledge answer to ground), which is a different truth from "this row
    # predates scoring". The evidence endpoint maps deterministic+unscored to
    # scoringStatus "not_applicable"; unscored rows without the marker stay
    # "not_scored_legacy" (pre-0051 rows keep the default false — no backfill can
    # honestly reconstruct their origin).
    deterministic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


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
    # Why it fired, in short owner-facing phrases (round 6 WP-G, F124). NULL on rows
    # from before the column existed and on server-originated signals.
    reasons: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    fired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # What happened to the owner notification for this row (2026-09-05 audit F079):
    # pending → delivered | owner_fallback | queued | failed. Rows from before the
    # column existed read ``unknown`` — never a fabricated ``delivered``.
    delivery_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="unknown"
    )
    delivery_detail: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The provider id of the send ``delivery_status`` rests on (round 7 WP-4b, migration
    # 0069): a Twilio status callback finds the row by it and stamps the real DLR.
    delivery_provider_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
