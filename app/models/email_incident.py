"""Email delivery incidents + escalation-contact verification state (round 5B #13).

One row per (tenant, email address), holding BOTH halves of "can this address be
trusted with a hot lead": bounce/complaint state written by the SES events
webhook, and the confirmation state written by the owner's verification loop.
SES accepting a message proves nothing — a typo'd-but-deliverable-looking
escalation contact used to swallow every hot-lead notification with a green
audit trail, because `accepted=True` suppressed the owner-email fallback and the
retry queue alike.

A sibling table rather than markers inside the escalation JSONB: the pillar PUT
is a strict full-replace the frontend echoes back, and the bounce writer is a
webhook racing it — the `notice_deliveries` upsert pattern is the established
answer. Suppression is DERIVED (``is_suppressed``): a hard bounce or complaint
suppresses until the address re-verifies after the incident.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin


class EmailDeliveryIncident(Base, TenantMixin, TimestampMixin):
    __tablename__ = "email_delivery_incidents"
    __table_args__ = (
        UniqueConstraint("admin_id", "email_lower", name="uq_email_incident_address"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Lower-cased address — the join key against the configured contacts.
    email_lower: Mapped[str] = mapped_column(String(320), nullable=False)
    # --- bounce / complaint state (webhook-written) --------------------------
    hard_bounced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    complained_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Transient bounces are recorded but non-gating (SES retries those itself).
    soft_bounce_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # e.g. "bounce:Permanent:General" — non-secret classification, never body text.
    last_event_detail: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # --- verification state (owner / confirm-route written) ------------------
    verification_pending_jti: Mapped[str | None] = mapped_column(String(64), nullable=True)
    verification_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def is_suppressed(row: EmailDeliveryIncident | None) -> bool:
    """Hard-bounced or complained, and not re-verified since the incident.

    Re-confirming the address is the un-suppress path: it proves the mailbox
    both delivers and is wanted."""
    if row is None:
        return False
    incidents = [t for t in (row.hard_bounced_at, row.complained_at) if t is not None]
    if not incidents:
        return False
    latest = max(incidents)
    return row.verified_at is None or row.verified_at < latest


def is_verified(row: EmailDeliveryIncident | None) -> bool:
    """Confirmed deliverable-and-wanted, with no newer incident."""
    return row is not None and row.verified_at is not None and not is_suppressed(row)
