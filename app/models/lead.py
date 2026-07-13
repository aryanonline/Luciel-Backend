"""Lead (end-customer) records — a tenant table, used here to prove cross-tenant
isolation on a second table during Arc 9 (full lead lifecycle lands in Arc 10/14).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, String, func
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
    intent: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    state: Mapped[LeadState] = mapped_column(
        Enum(LeadState, name="lead_state"), default=LeadState.active, nullable=False
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
