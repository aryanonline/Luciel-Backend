"""Declarative base + the TenantMixin that carries Layer-1 tenant scoping.

Three-layer tenant isolation (Architecture §3.7.2b, Space Instructions §3.1):
  Layer 1 (here): every tenant table carries a non-null ``admin_id``; the
    repository/query layer always filters by it. The mixin makes the column
    presence structural so a tenant table cannot be defined without it.
  Layer 2: Postgres RLS policy on every tenant table (see app/db/rls.py).
  Layer 3: per-request ``app.admin_id`` set once from the authenticated subject
    (see app/db/session.py).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Project-wide declarative base."""


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TenantMixin:
    """Marks a table as tenant-scoped and supplies the ``admin_id`` column.

    Any model that mixes this in is automatically picked up by the RLS policy
    generator (app/db/rls.py) and the isolation test suite, so a new tenant
    table cannot silently skip the three-layer defense.
    """

    admin_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)


def new_uuid() -> uuid.UUID:
    return uuid.uuid4()
