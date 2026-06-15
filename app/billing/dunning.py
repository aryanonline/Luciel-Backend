"""Failed-payment dunning state machine (Customer Journey §6, Legal §A3).

Single plan, monthly arrears. When a PAYG invoice payment fails, the account
enters a fixed dunning sequence rather than being cut off immediately:

  * ``none``     — no payment failure outstanding (steady state).
  * ``retrying`` — days 0–7 after the first failure. FULL service continues; the
    payment is retried on days 1/3/5/7 (CJ §6). The customer keeps PAYG behavior
    while Stripe retries the card.
  * ``reduced``  — day 7 reached with the payment still failing → revert to
    free-cap behavior (i.e. behave as if no card: cap at the free 50, at_cap data)
    until payment succeeds (Legal §A3).

A successful payment (Stripe ``invoice.paid``) clears dunning back to ``none``.

The schedule is measured from ``dunning_started_at`` (when the first failure was
recorded). Transitions are DRIVEN from the Stripe webhook handler (objective #4);
this module is the pure state logic so it is exhaustively unit-testable with an
injected clock — no wall-clock dependency.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# Retry days within the retrying window (CJ §6). FULL service through day 7.
RETRY_DAYS: tuple[int, ...] = (1, 3, 5, 7)
# Day the sequence ends in reduced (free-cap) behavior if still unpaid (CJ §6 / Legal §A3).
REDUCED_AFTER_DAYS: int = 7

NONE = "none"
RETRYING = "retrying"
REDUCED = "reduced"


def on_payment_failed(current_state: str, started_at: datetime | None) -> str:
    """A payment failed. Enter ``retrying`` if not already dunning; otherwise keep
    the current dunning state (the day-7 advance is time-driven, see :func:`advance`)."""
    # First failure → begin retrying (full service, days 0-7). Already-dunning stays
    # put; only the clock (advance) moves retrying → reduced (CJ §6).
    if current_state == NONE:
        return RETRYING
    return current_state


def on_payment_succeeded() -> str:
    """A payment succeeded (invoice.paid) → dunning clears (CJ §6)."""
    return NONE


def advance(current_state: str, started_at: datetime | None, now: datetime) -> str:
    """Time-driven transition: in ``retrying``, once day 7 is reached with payment
    still failing, drop to ``reduced`` = free-cap behavior (Legal §A3).

    ``started_at`` is when the first failure was recorded. Pure function of the
    clock so it is deterministic in tests (no wall-clock read)."""
    if current_state != RETRYING or started_at is None:
        return current_state
    if now >= started_at + timedelta(days=REDUCED_AFTER_DAYS):
        return REDUCED
    return current_state


def retry_dates(started_at: datetime) -> list[datetime]:
    """The scheduled retry instants (days 1/3/5/7 from the first failure, CJ §6).

    Surfaced for the webhook scheduler / tests to assert the exact schedule."""
    return [started_at + timedelta(days=d) for d in RETRY_DAYS]


def behaves_as_free_cap(dunning_state: str) -> bool:
    """In ``reduced``, the account behaves as free-cap regardless of card on file
    (Legal §A3) — drives the at_cap computation in metering."""
    return dunning_state == REDUCED
