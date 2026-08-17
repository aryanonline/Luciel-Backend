"""Booking events — the durable store behind "Appointments booked" (§3.9).

Arch §3.9 sources the appointments-booked metric from tool_calls in a trace
store that was never built, so the analytics card served an honest "not
available" forever. This table is the minimal durable fact the metric needs:
one row per SUCCESSFUL ``book_appointment`` dispatch, written by the tool
broker at the single point every dispatch already passes through — a metric
fed by the same event that told the customer "booked" can never disagree with
what Luciel said (§3.3.4 rule 1).

No lead PII: the booking reference is the calendar provider's event id; the
lead's email/name live on the calendar event and the lead record, not here.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin


class BookingEvent(Base, TenantMixin, TimestampMixin):
    """One successful booking made through Luciel."""

    __tablename__ = "booking_events"

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    instance_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # The provider's booking/event reference from the ok payload (§3.3.4).
    booking_ref: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    booked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
