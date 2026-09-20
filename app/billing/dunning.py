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

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.models.audit import ActorType
from app.models.billing import BillingAccount
from app.models.connection import InstanceConnection

# Retry days within the retrying window (CJ §6). FULL service through day 7.
RETRY_DAYS: tuple[int, ...] = (1, 3, 5, 7)
# Day the sequence ends in reduced (free-cap) behavior if still unpaid (CJ §6 / Legal §A3).
REDUCED_AFTER_DAYS: int = 7

NONE = "none"
RETRYING = "retrying"
REDUCED = "reduced"

# Action-tool connection types (Architecture §3.8.2 enum) that go dormant when
# dunning reaches ``reduced`` (Legal §A3: "Action-tool connections go dormant").
# Owner decision D1 (2026-09-14): ONLY true action tools. The BYO channel
# connections — ``email_sender`` (the owner's own mailbox) and ``sms_sender`` (the
# owner's own Twilio, which also carries voice; registry.DEPENDENT_CHANNELS) — stay
# live through every billing state, because the customer pays Microsoft/Twilio
# directly and the platform is not in that billing path; Legal §A3's "paid
# channels" phrase is being redlined out. ``channel_auth`` / ``instagram_auth`` /
# ``knowledge_source`` were never action tools and are likewise untouched.
ACTION_TOOL_CONNECTION_TYPES: frozenset[str] = frozenset(
    {"calendar", "crm", "record_source", "outbound_webhook"}
)


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


async def advance_if_due(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    now: datetime | None = None,
) -> str | None:
    """Apply the TIME-driven dunning transition for ONE tenant (CJ §6 / Legal §A3).

    :func:`advance` is otherwise only reached from the ``invoice.payment_failed``
    webhook, so a tenant whose card keeps failing but whose Stripe retries go quiet
    sat in ``retrying`` — full service, unpaid — indefinitely. This is the scheduled
    counterpart: it reads ``dunning_started_at`` and, once day 7 has passed, drops the
    account to ``reduced`` (free-cap behavior) and sends action-tool connections
    dormant, exactly as the webhook path does.

    Returns the new state when a transition happened, else None. Idempotent — a second
    run finds the account already ``reduced`` and changes nothing. Must be called
    inside a tenant-scoped session for ``admin_id`` so RLS scopes the read + write.
    """
    now = now or datetime.now(UTC)
    account = (
        await session.execute(select(BillingAccount).where(BillingAccount.admin_id == admin_id))
    ).scalar_one_or_none()
    if account is None:
        return None
    prev = account.dunning_state
    new_state = advance(prev, account.dunning_started_at, now)
    if new_state == prev:
        return None

    account.dunning_state = new_state
    await session.flush()
    await audit.emit(
        session,
        admin_id=admin_id,
        actor_type=ActorType.platform,
        event_type="dunning_state_changed",
        payload={"from": prev, "to": new_state, "reason": "dunning_schedule_elapsed"},
    )
    if new_state == REDUCED:
        await set_dormant_for_action_tools(session, admin_id=admin_id, reason="dunning_reduced")
    return new_state


async def set_dormant_for_action_tools(
    session: AsyncSession, *, admin_id: uuid.UUID, reason: str
) -> int:
    """Transition every connected action-tool connection to ``dormant`` (Legal §A3
    / Architecture §3.6.7): "Action-tool connections go dormant (credentials
    retained, not purged)".

    Fires on exactly ONE path: dunning reaching ``reduced`` (day-7 exhaustion,
    from the ``invoice.payment_failed`` webhook or the scheduled
    :func:`advance_if_due`). A VOLUNTARY card removal — the admin's DELETE
    payment-method call or the Stripe ``payment_method.detached`` webhook — does
    NOT dormant anything (owner decision D1, 2026-09-14: removing a payment method
    changes nothing but the 50-conversation cap). Scoped to the
    ACTION_TOOL_CONNECTION_TYPES set — BYO channel connections (email_sender,
    sms_sender), channel_auth and knowledge_source are NOT touched. Only rows
    currently ``connected`` move (an already-dormant/error/expired/revoked row is
    left alone — this is a downgrade, not a status reset). Idempotent: calling
    this twice in a row is a no-op the second time (no rows match).

    Emits ``connection_status_changed`` (Architecture §3.8.4) so the existing
    restore path (``billing.webhooks._restore_dormant_connections``) has
    something to restore. The prior status + detail are STASHED on the row
    (migration 0049 columns) so the restore returns the connection to exactly
    what it was — a billing dormancy never changed the credential's health.
    Returns the number of rows transitioned."""
    rows = (
        (
            await session.execute(
                select(InstanceConnection).where(
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.connection_type.in_(ACTION_TOOL_CONNECTION_TYPES),
                    InstanceConnection.status == "connected",
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.status_stashed = row.status
        row.status_detail_stashed = row.status_detail
        row.status = "dormant"
        row.status_detail = reason
    changed = len(rows)
    if changed > 0:
        await session.flush()
        await audit.emit(
            session,
            admin_id=admin_id,
            actor_type=ActorType.platform,
            event_type="connection_status_changed",
            payload={
                "reason": reason,
                "count": changed,
                "new_status": "dormant",
                "scope": "action_tool_connections",
            },
        )
    return changed
