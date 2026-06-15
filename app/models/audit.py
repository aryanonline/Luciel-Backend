"""admin_audit_log — append-only, SHA-256 hash-chained per admin_id.

Architecture §5.2 (schema + event enumeration) and §5.3 (immutability):
  - INSERT only; no UPDATE/DELETE grant to the app role (enforced in rls.py grants).
  - prev_hash = SHA-256 of the prior row's (event_id || created_at || event_payload)
    for the same admin_id; genesis row has prev_hash = NULL.
  - No raw lead PII in event_payload — only hashed resolved_lead_id + non-identifying
    metadata (Architecture §3.4.11 erasure-safe-by-design).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin


class ActorType(str, enum.Enum):
    admin_user = "admin_user"
    platform = "platform"
    luciel_runtime = "luciel_runtime"


class AdminAuditLog(Base, TenantMixin):
    __tablename__ = "admin_audit_log"

    event_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    actor_type: Mapped[ActorType] = mapped_column(
        Enum(ActorType, name="actor_type"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
