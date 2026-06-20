"""Stripe webhook event processing (Architecture §3.4.1b, CJ §6, Legal §A3).

Pure-ish handlers that mutate the tenant's BillingAccount + reset the budget
counter in response to verified Stripe events. The HTTP route
(app/api/v1/webhooks/stripe.py) verifies the signature FIRST (HARD CONSTRAINT #4),
then dispatches here. Idempotent on the Stripe event id (HARD CONSTRAINT, §3.4.1b):
a redelivered event is a no-op — Stripe may redeliver.

Events handled:
  * invoice.paid                    → reset period + counter (NO rollover), clear dunning,
                                      restore dormant connections (Legal §A3 / §3.6.7).
  * invoice.payment_failed          → drive dunning none→retrying, email notification +
                                      lifecycle_notice_sent audit (Legal §A3 / §3.6.7).
  * payment_method.attached         → flip billingState→payg_enabled + store display tuple,
                                      restore dormant connections (Legal §A3).
  * payment_method.detached         → flip billingState→free_cap + clear display tuple.
  * customer.subscription.renewed   → same as invoice.paid (§3.4.1b).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.billing import dunning
from app.billing.metering import get_or_create_billing_account
from app.billing.stripe_gateway import StripeEvent
from app.models.audit import ActorType
from app.models.billing import BillingAccount
from app.models.budget import ConversationBudget
from app.models.connection import InstanceConnection
from app.notifications import notifier as notify


async def _already_processed(account: BillingAccount, event_id: str) -> bool:
    """Idempotency guard: the last processed event id is stored on the account."""
    return account.last_stripe_event_id == event_id


async def handle_event(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    event: StripeEvent,
    now: datetime | None = None,
) -> str:
    """Dispatch one verified Stripe event for a tenant. Returns the action taken
    (or "ignored"/"duplicate"). Idempotent on event id (Stripe may redeliver)."""
    now = now or datetime.now(UTC)
    account = await get_or_create_billing_account(session, admin_id=admin_id)

    # Idempotent redelivery: same event id → no-op (HARD CONSTRAINT, §3.4.1b).
    if await _already_processed(account, event.event_id):
        return "duplicate"

    if event.type == "invoice.paid":
        action = await _on_invoice_paid(session, admin_id=admin_id, account=account, event=event)
    elif event.type == "invoice.payment_failed":
        action = await _on_payment_failed(session, admin_id=admin_id, account=account, now=now)
    elif event.type == "payment_method.attached":
        action = await _on_pm_attached(session, admin_id=admin_id, account=account, event=event)
    elif event.type == "payment_method.detached":
        action = await _on_pm_detached(session, admin_id=admin_id, account=account)
    elif event.type == "customer.subscription.renewed":
        # §3.4.1b: subscription renewal triggers same period-reset as invoice.paid.
        action = await _on_invoice_paid(session, admin_id=admin_id, account=account, event=event)
    else:
        action = "ignored"

    account.last_stripe_event_id = event.event_id
    await session.flush()
    return action


async def _on_invoice_paid(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    account: BillingAccount,
    event: StripeEvent,
) -> str:
    """invoice.paid → reset the period + counter (NO rollover), clear dunning,
    and restore dormant connections (Legal §A3 / Architecture §3.6.7).

    Period is Stripe-billing-date anchored (§3.4.1b): the new period bounds come
    from the invoice's period fields when present."""
    # Set the new Stripe-anchored period bounds (when the invoice carries them).
    ps = event.data.get("period_start")
    pe = event.data.get("period_end")
    if isinstance(ps, (int, float)):
        account.period_start = datetime.fromtimestamp(ps, tz=UTC)
    if isinstance(pe, (int, float)):
        account.period_resets_at = datetime.fromtimestamp(pe, tz=UTC)

    # Counter reset, NO rollover (§3.4.1b): drop the tenant's budget rows so the
    # new period starts at zero. The counter is the source of truth (budget.py).
    await session.execute(delete(ConversationBudget).where(ConversationBudget.admin_id == admin_id))

    # A successful payment clears any dunning sequence (CJ §6).
    prev_dunning = account.dunning_state
    account.dunning_state = dunning.on_payment_succeeded()
    account.dunning_started_at = None
    await session.flush()

    await audit.emit(
        session,
        admin_id=admin_id,
        actor_type=ActorType.platform,
        event_type="invoice_paid",
        payload={"reset": "period_and_counter", "rollover": False},
    )
    if prev_dunning != account.dunning_state:
        await audit.emit(
            session,
            admin_id=admin_id,
            actor_type=ActorType.platform,
            event_type="dunning_state_changed",
            payload={"from": prev_dunning, "to": account.dunning_state, "reason": "invoice_paid"},
        )

    # Legal §A3: restore dormant connections on successful payment.
    await _restore_dormant_connections(session, admin_id=admin_id)
    return "invoice_paid"


async def _on_payment_failed(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    account: BillingAccount,
    now: datetime,
) -> str:
    """invoice.payment_failed → enter/continue dunning + notify admin (CJ §6 / Legal §A3).

    First failure → retrying (full service, days 0–7). The day-7 → reduced advance
    is time-driven (dunning.advance), applied here against dunning_started_at.

    Architecture §3.6.7 / Legal §A3: "A notification is sent at each retry attempt"
    and "The cadence is logged to admin_audit_log (event_type = 'lifecycle_notice_sent')."
    """
    prev = account.dunning_state
    if account.dunning_started_at is None:
        account.dunning_started_at = now
    account.dunning_state = dunning.on_payment_failed(
        account.dunning_state, account.dunning_started_at
    )
    # Apply the time-driven advance (retrying → reduced once day 7 is reached).
    account.dunning_state = dunning.advance(account.dunning_state, account.dunning_started_at, now)

    # Compute the attempt number (1-based: which retry attempt this is, days 1/3/5/7).
    attempt_number = 1
    if account.dunning_started_at is not None:
        elapsed_days = (now - account.dunning_started_at).days
        retry_days = dunning.RETRY_DAYS  # (1, 3, 5, 7)
        # attempt_number = position in the retry schedule (1-indexed).
        attempt_number = sum(1 for d in retry_days if elapsed_days >= d) or 1

    await session.flush()

    await audit.emit(
        session,
        admin_id=admin_id,
        actor_type=ActorType.platform,
        event_type="payment_failed",
        payload={"dunning_state": account.dunning_state},
    )
    if prev != account.dunning_state:
        await audit.emit(
            session,
            admin_id=admin_id,
            actor_type=ActorType.platform,
            event_type="dunning_state_changed",
            payload={"from": prev, "to": account.dunning_state, "reason": "payment_failed"},
        )

    # Architecture §3.6.7 / Legal §A3: notify admin at each retry attempt and emit
    # lifecycle_notice_sent audit event.
    await notify.notify_payment_failed(
        session,
        admin_id=admin_id,
        dunning_state=account.dunning_state,
        attempt_number=attempt_number,
        now=now,
    )
    return "payment_failed"


async def _on_pm_attached(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    account: BillingAccount,
    event: StripeEvent,
) -> str:
    """payment_method.attached → payg_enabled + store the DISPLAY tuple only.

    HARD CONSTRAINT #2: only brand/last4/expMonth/expYear are read off the event —
    never the PAN. The card field on a Stripe PM object is a nested {brand,last4,
    exp_month,exp_year} display object; we copy exactly those four."""
    card = event.data.get("card") or {}
    if isinstance(card, dict):
        account.pm_brand = str(card.get("brand")) if card.get("brand") is not None else None
        account.pm_last4 = str(card.get("last4")) if card.get("last4") is not None else None
        em = card.get("exp_month")
        ey = card.get("exp_year")
        account.pm_exp_month = int(em) if isinstance(em, (int, float)) else None
        account.pm_exp_year = int(ey) if isinstance(ey, (int, float)) else None
    account.billing_state = "payg_enabled"  # adding a card flips behavior >50 (Vision §7)
    await session.flush()

    await audit.emit(
        session,
        admin_id=admin_id,
        actor_type=ActorType.platform,
        event_type="payment_method_added",
        payload={"brand": account.pm_brand, "last4": account.pm_last4},
    )

    # Legal §A3: "connections that go dormant … are restored automatically if a valid
    # payment method is added within the retention window."
    await _restore_dormant_connections(session, admin_id=admin_id)
    return "payment_method.attached"


async def _restore_dormant_connections(session: AsyncSession, *, admin_id: uuid.UUID) -> int:
    """Restore dormant connections to unconfigured for re-health-check (Legal §A3 / §3.6.7).

    When a valid payment method is added or invoice.paid fires after dunning, any
    connections placed in ``dormant`` state must be made re-checkable (unconfigured).
    The health worker will then probe them and promote to ``connected`` if healthy.

    Returns the number of rows transitioned."""
    result = await session.execute(
        update(InstanceConnection)
        .where(
            InstanceConnection.admin_id == admin_id,
            InstanceConnection.status == "dormant",
        )
        .values(status="unconfigured", status_detail="restored_after_payment")
    )
    restored = int(result.rowcount or 0)  # type: ignore[attr-defined]
    if restored > 0:
        await audit.emit(
            session,
            admin_id=admin_id,
            actor_type=ActorType.platform,
            event_type="connection_status_changed",
            payload={
                "reason": "dormant_connections_restored_on_payment",
                "count": restored,
                "new_status": "unconfigured",
            },
        )
    return restored


async def _on_pm_detached(
    session: AsyncSession, *, admin_id: uuid.UUID, account: BillingAccount
) -> str:
    """payment_method.detached → free_cap + clear the display tuple (Legal §A3)."""
    account.billing_state = "free_cap"
    account.pm_brand = None
    account.pm_last4 = None
    account.pm_exp_month = None
    account.pm_exp_year = None
    await session.flush()

    await audit.emit(
        session,
        admin_id=admin_id,
        actor_type=ActorType.platform,
        event_type="payment_method_removed",
        payload={"reason": "stripe_detached"},
    )
    return "payment_method.detached"
