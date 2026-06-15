"""Per-tenant billing account (Architecture §3.4.1b).

One row per tenant holding the Stripe binding + billing/dunning state + the
Stripe-billing-date-anchored period bounds + the DISPLAY-ONLY payment-method tuple.

Card hygiene (HARD CONSTRAINT #2, objective): the ONLY card fields here are the
display tuple — brand + last4 + expMonth + expYear. There is deliberately NO
column that can carry a PAN, CVV, or full card number; Stripe holds the card, we
hold pointers (stripe_customer_id / stripe_subscription_id) + the display tuple.

Period model (§3.4.1b / BUILD_SPEC ~196): the period is anchored on the Stripe
billing date, NOT the calendar month. ``period_resets_at`` is the Stripe period
end surfaced to the UI as ``periodResetsAt``; the counter resets on the Stripe
``invoice.paid`` webhook (no rollover).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin


class BillingAccount(Base, TenantMixin, TimestampMixin):
    """The tenant's billing binding + state (one per admin_id)."""

    __tablename__ = "billing_accounts"
    __table_args__ = (UniqueConstraint("admin_id", name="uq_billing_account_admin"),)

    id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Stripe pointers — never the card itself (§3.4.1b card hygiene).
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # billingState: free_cap (no card) | payg_enabled (card on file). Adding a card
    # flips behavior above the free 50 from at-cap-block to metered billing (Vision §7).
    billing_state: Mapped[str] = mapped_column(String(16), nullable=False, default="free_cap")
    # dunningState: none | retrying (days 0-7 full service) | reduced (day 7 →
    # free-cap behavior). Driven by Stripe payment-failure webhooks (CJ §6 / Legal §A3).
    dunning_state: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    # When the current dunning sequence began (a payment first failed); the day
    # schedule (retries 1/3/5/7, day-7 → reduced) is measured from here (CJ §6).
    dunning_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Stripe-billing-date-anchored period bounds (§3.4.1b — NOT calendar month).
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_resets_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Display-only payment-method tuple (§3.4.1b). NO PAN — these four fields are
    # the entire card surface the server ever holds or returns.
    pm_brand: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pm_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    pm_exp_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pm_exp_year: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Idempotency guard for Stripe webhook redelivery (§3.4.1b): the last processed
    # Stripe event id, so a redelivered event is a no-op (Stripe may redeliver).
    last_stripe_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
