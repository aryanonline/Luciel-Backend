"""Notice deliveries — the once-only ledger behind billing notice emails (§A5/§A10).

Legal §A5 owes an allowance reduction BOTH an email and a dashboard notice; §A10
price changes are the same shape, and Vision §7 promises a heads-up near the next
pay-as-you-go block. The emails are platform-initiated (a daily sweep, not a user
action), and schedules deliver at-least-once — so without a durable "this tenant
already got this notice" fact, every sweep run would re-send every active notice.

One row per (tenant, notice) is that fact. The unique constraint is the
idempotency guarantee: a concurrent or repeated sweep cannot double-send, because
only one insert wins. Rows are kept for the life of the account (they are the
proof a required notice went out, and pruning one would re-arm the send for a
notice that is still published); volume is bounded by construction — one row per
platform announcement plus at most one nudge per billing period.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin


class NoticeDelivery(Base, TenantMixin, TimestampMixin):
    """One billing-notice email this tenant has already been sent."""

    __tablename__ = "notice_deliveries"
    __table_args__ = (UniqueConstraint("admin_id", "notice_id", name="uq_notice_delivery"),)

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # The notice's stable identity: the derived/published noticeId for account
    # notices, ``payg80:<period_start>`` for the usage nudge. What "already sent"
    # is keyed on, so it must never change for the same announcement.
    notice_id: Mapped[str] = mapped_column(String(200), nullable=False)
    # allowance_reduction | price_change | terms_change | payg_usage_nudge
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    # sent (provider accepted) | recorded (Null sender — mock/CI posture). A
    # FAILED dispatch is deliberately NOT recorded here, so the next sweep
    # retries it; the failure itself is in the audit log.
    delivery_status: Mapped[str] = mapped_column(String(16), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
