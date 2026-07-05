"""deferred_actions — the bounded fail-safe retry queue (Architecture §3.8.7 rule D).

Deferred/outbound actions (``schedule_callback`` delivery §3.3.5, accepted-for-
delivery ``send_email``/``send_sms`` §3.3.6/§4.5) that hit an unhealthy delivery
connection at the moment of use are NOT failed silently and NOT dropped — they are
queued here and retried when the connection recovers (§3.8.7 rule D). Every item
carries a 24h max-age TTL; an item that cannot be delivered within its TTL is
abandoned, the owner is notified, and the event is audited (§3.8.7 rule D). The
queue therefore drains or expires — it never accumulates indefinitely.

Tenant-scoped (TenantMixin + RLS), so the isolation sweep auto-covers it and one
tenant can never see another's queued actions.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin


class DeferredAction(Base, TenantMixin, TimestampMixin):
    __tablename__ = "deferred_actions"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # The action class being deferred (e.g. schedule_callback, send_email, send_sms)
    # and the connection_type whose health gates its delivery (§3.8.7 rule D / A.2).
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    connection_type: Mapped[str] = mapped_column(String(32), nullable=False)
    # The resolved lead this action targets, when known (§3.3.5). Nullable: sessions
    # without a resolved identity (e.g. anonymous widget) carry None. Used to enforce
    # the schedule_callback per-lead cap; RLS + admin_id still scope every row.
    resolved_lead_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    # Non-secret payload the delivery needs (recipient, message, fire_at, etc.).
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    # queued | delivered | abandoned (§3.8.7 rule D terminal states).
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The 24h TTL deadline (§3.8.7 rule D): created_at + max-age. Past this, the item
    # is abandoned-with-notification on the next sweep rather than retried forever.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Next eligible retry time (exponential backoff, §3.8.7 rule D / §3.8.6 discipline).
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
