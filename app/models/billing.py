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
from datetime import UTC, date, datetime

from sqlalchemy import Date, DateTime, Integer, String, UniqueConstraint
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

    # High-water mark of billable 100-blocks accrued in the CURRENT period, and how many
    # of them Stripe has been told about. Both reset on invoice.paid with the counter.
    #
    # accrued_blocks never walks backwards within a period: build_budget reports zero
    # blocks the moment the card is gone, so without this a tenant who ran 250
    # conversations and then detached had two real blocks voided.
    #
    # last_reported_blocks is what makes a failed report recoverable: reporting is
    # "post when the accrual exceeds the watermark", so a Stripe blip is retried on the
    # next turn instead of losing the block forever.
    accrued_blocks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_reported_blocks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # The billing period the two watermarks above belong to (migration 0055). A period
    # can roll WITHOUT an invoice.paid (a failed invoice, a cancelled subscription, a
    # delayed webhook) — the counter is keyed by period and rolls on its own, but the
    # watermarks used to persist across the boundary, so the new period's first blocks
    # were never reported until they exceeded the OLD period's total. Metering compares
    # this to the current period and rolls the watermarks (flushing any residual under
    # the old period's identifiers first).
    accrual_period_start: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Monotonic ALL-TIME billed-conversation count (migration 0035). Incremented in
    # count_session_once inside the same transaction as the counted_sessions claim
    # (fire-once inherited), and — unlike the per-period ledger — NEVER reset by
    # invoice.paid. This is what lets analytics say "all-time" honestly while the
    # ledger keeps its correct period-reset semantics.
    lifetime_conversations: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Display-only payment-method tuple (§3.4.1b). NO PAN — these four fields are
    # the entire card surface the server ever holds or returns.
    pm_brand: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pm_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    pm_exp_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pm_exp_year: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Superseded by processed_stripe_events as the redelivery guard: a single column is a
    # ONE-DEEP window, so an A → B → A redelivery reprocessed A. Retained as the "last
    # event we saw" breadcrumb for operators reading the account row.
    last_stripe_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ProcessedStripeEvent(Base, TenantMixin, TimestampMixin):
    """Every Stripe event id already applied — the redelivery guard (§3.4.1b).

    The event id is the primary key, so ``INSERT ... ON CONFLICT DO NOTHING`` is the
    whole gate: the rowcount says whether this delivery is the first. Replay is not
    hypothetical — Stripe redelivers on any non-2xx, and each replayed ``invoice.paid``
    used to delete the tenant's usage counter, handing back a fresh free 50 and losing
    every conversation run since the invoice.
    """

    __tablename__ = "processed_stripe_events"

    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
