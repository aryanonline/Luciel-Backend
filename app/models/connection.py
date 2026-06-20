"""instance_connections (Architecture §3.8.2). The one model for everything external.

Secret material NEVER lives here — only ``secret_ref`` (a Secrets Manager ARN /
SSM path pointer) and ``non_secret_config`` (Architecture §3.8.3).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin


class InstanceConnection(Base, TenantMixin, TimestampMixin):
    __tablename__ = "instance_connections"

    connection_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    connection_type: Mapped[str] = mapped_column(String(32), nullable=False)
    auth_class: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="unconfigured")
    secret_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    non_secret_config: Mapped[dict[str, object] | None] = mapped_column(JSONB, nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True), nullable=True
    )
    last_health_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
