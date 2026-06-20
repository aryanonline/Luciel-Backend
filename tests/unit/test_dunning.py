"""Dunning state machine (Customer Journey §6, Legal §A3).

none → retrying (day 0, on first payment failure, FULL service, retries 1/3/5/7)
→ reduced (day 7 reached, still failing → free-cap behavior). invoice.paid clears
back to none.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.billing import dunning


def test_first_failure_enters_retrying() -> None:
    assert dunning.on_payment_failed(dunning.NONE, None) == dunning.RETRYING


def test_subsequent_failure_keeps_state() -> None:
    # Already retrying stays retrying (the clock, not the event, moves to reduced).
    assert dunning.on_payment_failed(dunning.RETRYING, datetime.now(UTC)) == dunning.RETRYING


def test_payment_success_clears_to_none() -> None:
    assert dunning.on_payment_succeeded() == dunning.NONE


def test_retry_schedule_is_days_1_3_5_7() -> None:
    start = datetime(2026, 6, 1, tzinfo=UTC)
    dates = dunning.retry_dates(start)
    assert dates == [start + timedelta(days=d) for d in (1, 3, 5, 7)]


def test_advance_stays_retrying_before_day_7() -> None:
    start = datetime(2026, 6, 1, tzinfo=UTC)
    # Day 6 → still retrying, full service (CJ §6).
    assert dunning.advance(dunning.RETRYING, start, start + timedelta(days=6)) == dunning.RETRYING


def test_advance_to_reduced_at_day_7() -> None:
    start = datetime(2026, 6, 1, tzinfo=UTC)
    # Day 7 reached → reduced = free-cap behavior (Legal §A3).
    assert dunning.advance(dunning.RETRYING, start, start + timedelta(days=7)) == dunning.REDUCED


def test_reduced_behaves_as_free_cap() -> None:
    assert dunning.behaves_as_free_cap(dunning.REDUCED) is True
    assert dunning.behaves_as_free_cap(dunning.RETRYING) is False
    assert dunning.behaves_as_free_cap(dunning.NONE) is False


# ---------------------------------------------------------------------------
# Regression tests for Axis8-F03 (lifecycle_notice_sent audit event)
# ---------------------------------------------------------------------------


def test_dunning_module_exports_retry_days() -> None:
    """Axis8-F03: RETRY_DAYS constant is available for attempt calculation."""
    assert dunning.RETRY_DAYS == (1, 3, 5, 7)
