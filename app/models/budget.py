"""Conversation budget counter (Architecture §3.4.1b).

The monthly budget counter keyed by (admin_id, instance_id, billing_period_start).
Postgres is the durable source of truth; Redis is the hot read/write cache
(§3.4.1b storage model). Full metering (Stripe overage, period reset on
invoice.paid) is Arc 18 — Arc 14 needs only the gate, so this table holds the
durable count and the set of session ids already counted for idempotency.

The counted-session set makes the increment idempotent on the session identifier
(§3.4.1b Counter precision): neither the first-model-call path nor the
human-takeover-before-model path can double-count the same session.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import Date, Integer, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin


class ConversationBudget(Base, TenantMixin, TimestampMixin):
    """Per-(tenant, instance, billing period) conversation counter — source of truth."""

    __tablename__ = "conversation_budgets"
    __table_args__ = (
        UniqueConstraint(
            "admin_id",
            "instance_id",
            "billing_period_start",
            name="uq_budget_period",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # Billing period anchor; resets on Stripe invoice.paid in Arc 18 (no calendar
    # boundary reset — §3.4.1b). Stored as a date for the current-period key.
    billing_period_start: Mapped[date] = mapped_column(Date, nullable=False)
    # Conversations counted this period (free + billed). The budget unit is
    # conversations, not LLM calls (§3.4.1b).
    counted_conversations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Superseded by the counted_sessions table as the fire-once guard: a JSONB list can
    # only be updated by read-modify-write, so two concurrent turns on one session both
    # saw the id absent and both counted. Retained because it is the historical record of
    # which sessions were counted before that table existed.
    counted_session_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)


class CountedSession(Base, TenantMixin, TimestampMixin):
    """One row per conversation counted — the fire-once guard (§3.4.1b Counter precision).

    The uniqueness of (admin_id, instance_id, session_id) is what makes the increment
    idempotent, and Postgres enforces it: the caller inserts with ``ON CONFLICT DO
    NOTHING`` and the rowcount says whether this call is the one that counted.
    Concurrency is then the database's problem rather than a lost update.

    The period is recorded but deliberately NOT part of the key (migration 0055): a
    session that straddles a period boundary is still ONE conversation — keying on the
    period counted it once in each (audit F029), against the "counted once" rule.
    """

    __tablename__ = "counted_sessions"
    __table_args__ = (
        UniqueConstraint(
            "admin_id",
            "instance_id",
            "session_id",
            name="uq_counted_session_once",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    billing_period_start: Mapped[date] = mapped_column(Date, nullable=False)
