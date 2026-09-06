"""Lead (end-customer) records — a tenant table, used here to prove cross-tenant
isolation on a second table during Arc 9 (full lead lifecycle lands in Arc 10/14).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, Float, Index, String, UniqueConstraint, func
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
    # 2026-09-05 audit F111: the lead's volunteered phone, E.164, when the transport
    # key is something else (an email-keyed visitor who typed their number). Fill-
    # when-absent like ``email``; NULL when unknown or when contact_identifier IS it.
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
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
    # 2026-09-05 audit F134: the honest CRM state of this record — ``synced`` after
    # a successful push, ``failed`` with a non-secret reason when the CRM refused or
    # could not be reached, NULL when nothing was ever attempted. The owner is
    # emailed ONCE per lead about a failure (``crm_failure_notified_at``).
    crm_push_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    crm_push_detail: Mapped[str | None] = mapped_column(String(160), nullable=True)
    crm_pushed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    crm_failure_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class LeadIdentifier(Base, TenantMixin, TimestampMixin):
    """Every identifier a lead has ever been known by (2026-09-05 audit F125/F127).

    One row per (kind, value) per tenant instance: ``email`` (case-folded), ``phone``
    (E.164) or ``transport`` (a Meta page-scoped id and the like). Unifying two
    leads re-points the absorbed row's identifiers here, so a person who first
    wrote from one address and later from another keeps resolving to ONE record
    from either — the old two-column lookup forgot the absorbed identity."""

    __tablename__ = "lead_identifiers"
    __table_args__ = (
        UniqueConstraint("admin_id", "instance_id", "kind", "value", name="uq_lead_identifier"),
        Index("ix_lead_identifiers_lead", "admin_id", "lead_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    lead_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[str] = mapped_column(String(320), nullable=False)
