"""Internal access log write API (Architecture §5.11 / Commitment §23).

Provides ``emit_internal_access()`` — the only function that writes to
``internal_access_log``. Enforces the append-only contract at the application layer
(no update/delete path provided) on top of the DB-level INSERT-only grant applied
in migration 0012.

Usage:
  await emit_internal_access(
      session,
      staff_id="ops-eng@example.com",
      approver_id="lead-eng@example.com",
      admin_id=<uuid>,
      data_class="leads",
      access_started_at=now,
      access_type="break_glass",
      justification="Customer-reported data issue, ticket #1234",
      ticket_ref="#1234",
  )
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.internal_access import (
    ACCESS_TYPE_BREAK_GLASS,
    ACCESS_TYPE_SUPPORT_SCOPED,
    InternalAccessLog,
)

__all__ = [
    "emit_internal_access",
    "ACCESS_TYPE_BREAK_GLASS",
    "ACCESS_TYPE_SUPPORT_SCOPED",
]

_VALID_ACCESS_TYPES = frozenset({ACCESS_TYPE_BREAK_GLASS, ACCESS_TYPE_SUPPORT_SCOPED})


async def emit_internal_access(
    session: AsyncSession,
    *,
    staff_id: str,
    approver_id: str | None = None,
    admin_id: uuid.UUID | None = None,
    instance_id: uuid.UUID | None = None,
    data_class: str | None = None,
    access_started_at: datetime | None = None,
    access_ended_at: datetime | None = None,
    access_type: str,
    justification: str | None = None,
    ticket_ref: str | None = None,
) -> InternalAccessLog:
    """Write one entry to the internal_access_log (Architecture §5.11, append-only).

    This is the sole write path; there is no update/delete function.  The DB-level
    INSERT-only grant (migration 0012) enforces this at the Postgres layer.

    Args:
        staff_id: Identifier of the acting staff member (email, SSO subject, etc.).
        approver_id: Second-person approver (required for break_glass in practice,
            validated operationally; this function accepts None for support_scoped).
        admin_id: Tenant whose data was accessed (nullable for platform-wide ops).
        instance_id: Specific Luciel instance (nullable).
        data_class: Category of data accessed (e.g. "leads", "transcripts", "secrets").
        access_started_at: Start of the access window; defaults to now (UTC).
        access_ended_at: End of the access window; nullable if still in progress.
        access_type: ``break_glass`` or ``support_scoped`` (see §5.11).
        justification: Free-text reason for the access (required for break_glass).
        ticket_ref: Linked support/ops ticket reference.

    Returns the persisted ``InternalAccessLog`` row (flushed but not committed).
    Raises ``ValueError`` for unknown access_type.
    """
    if access_type not in _VALID_ACCESS_TYPES:
        raise ValueError(
            f"Invalid access_type {access_type!r}; must be one of {sorted(_VALID_ACCESS_TYPES)}"
        )

    now = datetime.now(UTC)
    entry = InternalAccessLog(
        id=uuid.uuid4(),
        staff_id=staff_id,
        approver_id=approver_id,
        admin_id=admin_id,
        instance_id=instance_id,
        data_class=data_class,
        access_started_at=access_started_at or now,
        access_ended_at=access_ended_at,
        access_type=access_type,
        justification=justification,
        ticket_ref=ticket_ref,
    )
    session.add(entry)
    await session.flush()
    return entry
