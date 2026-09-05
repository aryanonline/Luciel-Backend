"""The billing-period clock (§3.4.1b) — pure functions, no database.

The free 50 is "per Stripe billing period" once billing exists and "per calendar month"
before that. ``current_period`` is the one place that decides which period an instant
falls in, and it must keep moving when Stripe goes quiet (audit F030).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.billing.stripe_gateway import add_months
from app.models.billing import BillingAccount
from app.runtime.budget import current_period, period_start_for


def _account(start: datetime | None, resets: datetime | None = None) -> BillingAccount:
    return BillingAccount(billing_state="payg_enabled", period_start=start, period_resets_at=resets)


def test_calendar_month_before_any_stripe_period():
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    assert current_period(None, now) == (date(2026, 9, 1), datetime(2026, 10, 1, tzinfo=UTC))
    assert current_period(_account(None), now) == (
        date(2026, 9, 1),
        datetime(2026, 10, 1, tzinfo=UTC),
    )
    # December rolls the year.
    assert current_period(None, datetime(2026, 12, 31, tzinfo=UTC))[1] == datetime(
        2027, 1, 1, tzinfo=UTC
    )


def test_inside_the_anchored_period_nothing_moves():
    acct = _account(datetime(2026, 8, 15, tzinfo=UTC), datetime(2026, 9, 15, tzinfo=UTC))
    assert current_period(acct, datetime(2026, 9, 5, tzinfo=UTC)) == (
        date(2026, 8, 15),
        datetime(2026, 9, 15, tzinfo=UTC),
    )
    assert period_start_for(acct, datetime(2026, 9, 5, tzinfo=UTC)) == date(2026, 8, 15)


def test_a_period_that_ended_without_an_invoice_rolls_forward_whole_cycles():
    """F030: the anchor froze the first time invoice.paid never came, so the free 50
    stopped resetting. It now advances cycle by cycle from the last known anchor."""
    acct = _account(datetime(2026, 1, 15, tzinfo=UTC), datetime(2026, 2, 15, tzinfo=UTC))
    assert current_period(acct, datetime(2026, 2, 15, tzinfo=UTC)) == (
        date(2026, 2, 15),
        datetime(2026, 3, 15, tzinfo=UTC),
    )
    assert current_period(acct, datetime(2026, 9, 5, tzinfo=UTC)) == (
        date(2026, 8, 15),
        datetime(2026, 9, 15, tzinfo=UTC),
    )


def test_a_missing_or_inverted_reset_is_repaired_from_the_anchor():
    acct = _account(datetime(2026, 8, 15, tzinfo=UTC), None)
    assert current_period(acct, datetime(2026, 9, 5, tzinfo=UTC))[1] == datetime(
        2026, 9, 15, tzinfo=UTC
    )
    inverted = _account(datetime(2026, 8, 15, tzinfo=UTC), datetime(2026, 8, 1, tzinfo=UTC))
    assert current_period(inverted, datetime(2026, 9, 5, tzinfo=UTC)) == (
        date(2026, 8, 15),
        datetime(2026, 9, 15, tzinfo=UTC),
    )


def test_add_months_clamps_like_a_stripe_billing_anchor():
    assert add_months(datetime(2026, 1, 31, tzinfo=UTC), 1) == datetime(2026, 2, 28, tzinfo=UTC)
    assert add_months(datetime(2028, 1, 31, tzinfo=UTC), 1) == datetime(2028, 2, 29, tzinfo=UTC)
    assert add_months(datetime(2026, 12, 15, tzinfo=UTC), 1) == datetime(2027, 1, 15, tzinfo=UTC)
    assert add_months(datetime(2026, 3, 31, tzinfo=UTC), 1) == datetime(2026, 4, 30, tzinfo=UTC)
