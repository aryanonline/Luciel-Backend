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

from sqlalchemy import DateTime, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin


class CrmLeadLink(Base, TenantMixin, TimestampMixin):
    """Where one lead's record lives in the tenant's CRM (per provider)."""

    __tablename__ = "crm_lead_links"
    __table_args__ = (
        UniqueConstraint(
            "admin_id", "instance_id", "provider", "lead_key", name="uq_crm_lead_link"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # CRM provider the record lives in (hubspot | salesforce | custom_webhook).
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    # The stable lead key the runtime pushes under (the resolved lead's dedupe
    # identity — e-mail today). What makes "the same lead" the same lead.
    lead_key: Mapped[str] = mapped_column(String(320), nullable=False)
    # The provider's record id our first push created (or found). All later
    # pushes update THIS record in place.
    crm_record_id: Mapped[str] = mapped_column(String(128), nullable=False)
    last_pushed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
