"""CRM lead links — the persistence behind "maintains the SAME lead's record".

§3.3.6 (owner-reconfirmed): ``push_to_crm`` is an idempotent upsert that keeps
one lead's CRM record current over the lead's lifetime — update-in-place, never
a duplicate. Search-by-email alone cannot honor that: the moment a lead's email
changes (or the CRM merges contacts), a later push would create a second record.
This table pins the mapping ``lead_key → crm_record_id`` per provider, so a
subsequent push PATCHes the exact record the first push created, whatever the
lead's identifying fields say now. A 404 on the linked id (record deleted in the
CRM) falls back to search/create and the link heals.

Not a two-way sync (§3.3.6 scope boundary): rows record where OUR pushes landed;
nothing here reads the CRM back.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, String, func, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin


class CrmLeadLink(Base, TenantMixin, TimestampMixin):
    """Where one lead's record lives in the tenant's CRM (per provider)."""

    __tablename__ = "crm_lead_links"
    __table_args__ = (
        # Round 5A item 9 (migration 0053): the REAL invariant is one CRM record
        # per LEAD per provider — lead_key is mutable once emails can change, so
        # the old unique-on-key constraint could only ever pin the wrong thing.
        Index(
            "uq_crm_lead_link_lead",
            "admin_id",
            "instance_id",
            "provider",
            "lead_id",
            unique=True,
            postgresql_where=text("lead_id IS NOT NULL"),
        ),
        Index("ix_crm_lead_links_key", "admin_id", "instance_id", "provider", "lead_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # CRM provider the record lives in (hubspot | salesforce | custom_webhook).
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    # The platform Lead this link belongs to (round 5A item 9, migration 0053).
    # Nullable: legacy rows are adopted lazily — lead_id is stamped on the next
    # successful push whose resolved lead OWNS the key. This — not lead_key — is
    # what makes "the link survives an email change" actually true.
    lead_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    # The lead key of the LAST SUCCESSFUL push (e-mail). Refreshed on every
    # touch; a mismatch with the incoming key is the "the identity field moved —
    # tell the CRM" signal.
    lead_key: Mapped[str] = mapped_column(String(320), nullable=False)
    # The provider's record id our first push created (or found). All later
    # pushes update THIS record in place.
    crm_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    last_pushed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
