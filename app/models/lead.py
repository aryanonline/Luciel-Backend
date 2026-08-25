"""Lead (end-customer) records — a tenant table, used here to prove cross-tenant
isolation on a second table during Arc 9 (full lead lifecycle lands in Arc 10/14).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Float, String, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin


class LeadState(str, enum.Enum):
    active = "active"
    archived = "archived"
    # No 'pruned' — prune permanently deletes (Architecture §3.4.10a).


class Lead(Base, TenantMixin, TimestampMixin):
    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    contact_identifier: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Round 5A item 5 (migration 0052): the lead's volunteered email when the
    # TRANSPORT key is a phone/meta id. An SMS caller who later says "email me at
    # jane@…" used to have that email silently DISCARDED (contact_identifier was
    # already set and nothing else stored it) — with it, the CRM push gains its
    # email dedupe key and a phone-keyed lead finally reaches the tenant's CRM.
    # NULL when unknown or when contact_identifier already IS the email.
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    intent: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    # Normalized high-value-lead score at capture time (§3.4.5); nullable when a lead
    # was captured without a scored high-value signal.
    lead_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Channel opt-out (STOP/HELP for SMS, §3.1.6). An opted-out lead is NEVER sent a
    # scheduled callback regardless of caps (§3.3.5) — read by the callback dispatcher.
    opted_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    state: Mapped[LeadState] = mapped_column(
        Enum(LeadState, name="lead_state"), default=LeadState.active, nullable=False
    )
    # Admin-marked business outcome (Vision §7 "where the admin marks a lead
    # outcome"; audit round 3 C14, migration 0050). Distinct from ``state``
    # (active/archived — a VISIBILITY lifecycle): outcome is what the analytics
    # conversion metrics group by. Wire values, CHECK-constrained in the migration.
    outcome: Mapped[str] = mapped_column(
        String(16), nullable=False, default="in_progress", server_default="in_progress"
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
