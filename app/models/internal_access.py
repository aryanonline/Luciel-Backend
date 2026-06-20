"""InternalAccessLog — append-only staff-access audit artifact (Architecture §5.11).

Architecture §5.11 (C23 / Legal §C9): "The internal access log is a concrete,
append-only artifact, not a placeholder. Each entry records: the acting staff
identity; the second-person approver identity; the admin_id / instance_id and data
class accessed; the access start and end timestamps; the justification and the
linked ticket reference; and the access type (break_glass | support_scoped)."

Separate from ``admin_audit_log`` (§5.11 explicitly requires this separation):
  - ``admin_audit_log`` captures *tenant* actions (login, config, billing, etc.).
  - ``internal_access_log`` captures *platform staff* access events for internal
    compliance/auditing (no-standing-access / break-glass policy, §5.11).

Append-only enforcement:
  - The application role (``luciel_app``) is granted INSERT only, no UPDATE/DELETE.
  - Migration 0012 applies the INSERT-only grant and adds a NO-UPDATE trigger
    consistent with the audit-log guard pattern (B1 Axis9-F01 / §5.3).

Retention: ≥ 3 years (§5.11).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

# Valid access types (§5.11).
ACCESS_TYPE_BREAK_GLASS = "break_glass"
ACCESS_TYPE_SUPPORT_SCOPED = "support_scoped"


class InternalAccessLog(Base, TimestampMixin):
    """Platform staff access record — append-only, separate from admin_audit_log.

    NOT a TenantMixin: this table is NOT tenant-scoped (it captures cross-tenant
    staff actions). INSERT-only for the app role; no RLS (staff table, not tenant)."""

    __tablename__ = "internal_access_log"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Staff identity — acting person and approver (second-person requirement, §5.11).
    staff_id: Mapped[str] = mapped_column(String(255), nullable=False)
    approver_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Scope: which tenant / instance / data class was accessed.
    # Nullable: not all access events are scoped to a single tenant.
    admin_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    instance_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    data_class: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Time bounds — start and end of the access window (§5.11).
    access_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    access_ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Access type: break_glass | support_scoped (§5.11).
    access_type: Mapped[str] = mapped_column(String(32), nullable=False)

    # Justification + linked ticket (§5.11 mandatory for break_glass).
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    ticket_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
