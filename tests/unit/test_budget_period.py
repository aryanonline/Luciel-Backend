"""The billing-period clock (§3.4.1b) — pure functions, no database.

The free 50 is "per Stripe billing period" once billing exists and "per calendar month"
before that. ``current_period`` is the one place that decides which period an instant
falls in, and it must keep moving when Stripe goes quiet (audit F030).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.billing.stripe_gateway import add_months, next_period_end
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


def _utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def test_add_months_steps_a_clamped_date_on_the_anchors_day():
    """A clamped date has lost its anchor's day: Feb 28 + 1 month is Mar 28, while the
    renewal after a Jan-31 anchor's Feb 28 one is Mar 31. ``day`` carries the anchor's."""
    assert add_months(_utc(2030, 2, 28), 1) == _utc(2030, 3, 28)
    assert add_months(_utc(2030, 2, 28), 1, day=31) == _utc(2030, 3, 31)
    assert add_months(_utc(2030, 4, 30), 1, day=31) == _utc(2030, 5, 31)
    assert add_months(_utc(2032, 1, 30), 1, day=30) == _utc(2032, 2, 29)  # leap year
    assert add_months(_utc(2030, 1, 31, 14, 5), 0, day=31) == _utc(2030, 1, 31, 14, 5)


def test_the_next_period_keeps_stripes_anchor_day():
    """docs.stripe.com/billing/subscriptions/billing-cycle: a Jan-31 anchor bills Feb 28
    (Feb 29 in a leap year), then Mar 31, Apr 30. The day is read off the pair of
    boundaries, the time of day is kept, and a pair that is not one whole monthly period
    (a prorated first period) steps one plain month."""
    assert next_period_end(_utc(2030, 1, 31), _utc(2030, 2, 28)) == _utc(2030, 3, 31)
    assert next_period_end(_utc(2030, 2, 28), _utc(2030, 3, 31)) == _utc(2030, 4, 30)
    assert next_period_end(_utc(2030, 3, 31), _utc(2030, 4, 30)) == _utc(2030, 5, 31)
    assert next_period_end(_utc(2032, 1, 31), _utc(2032, 2, 29)) == _utc(2032, 3, 31)
    assert next_period_end(_utc(2030, 1, 30), _utc(2030, 2, 28)) == _utc(2030, 3, 30)
    assert next_period_end(_utc(2030, 1, 29), _utc(2030, 2, 28)) == _utc(2030, 3, 29)
    assert next_period_end(_utc(2030, 2, 28), _utc(2030, 3, 28)) == _utc(2030, 4, 28)
    assert next_period_end(_utc(2030, 11, 30), _utc(2030, 12, 31)) == _utc(2031, 1, 31)
    assert next_period_end(_utc(2030, 1, 15), _utc(2030, 2, 15)) == _utc(2030, 3, 15)
    assert next_period_end(_utc(2030, 1, 31, 14, 5, 9), _utc(2030, 2, 28, 14, 5, 9)) == _utc(
        2030, 3, 31, 14, 5, 9
    )
    assert next_period_end(_utc(2030, 1, 10), _utc(2030, 2, 1)) == _utc(2030, 3, 1)


def test_a_month_end_anchor_rolls_on_stripes_dates():
    """The roll used to chain add_months on its own clamped result: a Jan-31 anchor rolled
    to Feb 28 and then Mar 28, so from Mar 28 to Mar 31 the counter sat in a period
    Stripe never opened, with a second free 50, and periodResetsAt named the wrong day."""
    bound = _account(_utc(2030, 1, 31), _utc(2030, 2, 28))
    assert current_period(bound, _utc(2030, 3, 29)) == (date(2030, 2, 28), _utc(2030, 3, 31))
    assert current_period(bound, _utc(2030, 3, 31)) == (date(2030, 3, 31), _utc(2030, 4, 30))
    assert current_period(bound, _utc(2030, 5, 1)) == (date(2030, 4, 30), _utc(2030, 5, 31))
    renewed = _account(_utc(2030, 2, 28), _utc(2030, 3, 31))
    assert current_period(renewed, _utc(2030, 3, 29)) == (date(2030, 2, 28), _utc(2030, 3, 31))
    assert current_period(renewed, _utc(2030, 4, 30)) == (date(2030, 4, 30), _utc(2030, 5, 31))
