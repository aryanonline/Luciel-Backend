"""Record-source rows — the live store behind ``lookup_record``'s CSV provider.

§3.2 boundary (owner-reconfirmed): the knowledge base is Luciel's memory; the
``lookup_record`` tool answers from a LIVE, structured system of record. For the
``csv`` provider the admin's uploaded spreadsheet IS that system of record, and
this table is its storage: the tool queries it at call time — never a knowledge
chunk, never a cached snapshot — so a re-upload is immediately what the tool
serves (upload REPLACES the tenant's rows).

TenantMixin: RLS + the isolation sweep + the delete cascade cover it like every
other tenant table.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin


class RecordSourceRecord(Base, TenantMixin, TimestampMixin):
    """One row of the tenant's uploaded record source (CSV provider)."""

    __tablename__ = "record_source_records"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # Ordinal within the upload — stable ordering for ambiguous-match listings.
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The row's fields, keyed by the CSV header names (admin-defined schema —
    # the platform imposes no vertical ontology, §3.2.1).
    fields: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
