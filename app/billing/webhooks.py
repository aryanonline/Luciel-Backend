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

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.billing import dunning, metering
from app.billing.metering import get_or_create_billing_account
from app.billing.stripe_gateway import StripeEvent
from app.models.audit import ActorType
from app.models.billing import BillingAccount, ProcessedStripeEvent
from app.models.budget import ConversationBudget, CountedSession
from app.models.connection import InstanceConnection
from app.notifications import notifier as notify

_log = logging.getLogger(__name__)


async def _claim_event(session: AsyncSession, *, admin_id: uuid.UUID, event_id: str) -> bool:
    """Claim this event id for processing. False if it was already applied.

    The gate is the ``processed_stripe_events`` primary key rather than a comparison
    against ``last_stripe_event_id``: that was a one-deep window, so an A → B → A
    redelivery reprocessed A. Only the audit emissions had been made replay-safe — the
    state mutations had not, and every one of them is destructive on a second
    application (the counter is wiped, a re-carded account is flipped back to free_cap
    and its tools re-dormanted, dunning re-advances and re-emails the admin).
    """
    claimed = await session.execute(
        pg_insert(ProcessedStripeEvent)
        .values(admin_id=admin_id, event_id=event_id, processed_at=datetime.now(UTC))
        .on_conflict_do_nothing(index_elements=["event_id"])
    )
    return bool(claimed.rowcount)  # type: ignore[attr-defined]


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

    # Idempotent redelivery: an event id already applied → no-op (HARD CONSTRAINT,
    # §3.4.1b). Claimed BEFORE any state mutation, so a replay cannot re-apply one.
    if not await _claim_event(session, admin_id=admin_id, event_id=event.event_id):
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
    # The fire-once guard is keyed on the period too, so its rows are spent along with
    # the counter they guarded — leaving them would only accumulate.
    await session.execute(delete(CountedSession).where(CountedSession.admin_id == admin_id))

    # The usage watermark is period-scoped like the counter: the invoice just settled
    # everything reported so far, so the new period starts owing Stripe nothing.
    account.accrued_blocks = 0
    account.last_reported_blocks = 0

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
        # Legal §A3 / Architecture §3.6.7: reaching `reduced` (day-7 exhaustion) sends
        # action-tool connections dormant (credentials retained, not purged).
        if account.dunning_state == dunning.REDUCED:
            await dunning.set_dormant_for_action_tools(
                session, admin_id=admin_id, reason="dunning_reduced"
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
    prev_billing_state = account.billing_state
    account.billing_state = "payg_enabled"  # adding a card flips behavior >50 (Vision §7)
    await session.flush()

    await audit.emit(
        session,
        admin_id=admin_id,
        actor_type=ActorType.platform,
        event_type="payment_method_added",
        payload={"brand": account.pm_brand, "last4": account.pm_last4},
    )
    # Architecture §3.6.7 / §5.2: the billing state change itself is the canonical
    # lifecycle_transition (free_cap → payg_enabled). payment_method_added above is
    # supplementary detail. Emit only on an actual state change so idempotent webhook
    # redelivery does not log a spurious transition (audit finding Axis3-F04).
    if prev_billing_state != "payg_enabled":
        await audit.emit(
            session,
            admin_id=admin_id,
            actor_type=ActorType.platform,
            event_type="lifecycle_transition",
            payload={"from": prev_billing_state, "to": "payg_enabled", "trigger": "payment_method"},
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
    """payment_method.detached → free_cap + clear the display tuple (Legal §A3).

    Also sends action-tool connections dormant (Legal §A3: losing a payment
    method reverts to free-cap behavior AND "Action-tool connections go dormant").
    """
    # Bill what they already ran BEFORE the account stops looking billable: once
    # billing_state is free_cap, build_budget reports zero blocks and report_usage
    # refuses the account, so any accrued overage would simply vanish (P1-13).
    try:
        await metering.report_accrued_usage(session, admin_id=admin_id)
    except Exception:  # noqa: BLE001 — a Stripe blip must not strand the detach event
        # The watermark is unmoved, so the reconcile sweep picks this tenant up again.
        _log.exception("accrued usage flush failed at detach", extra={"admin_id": str(admin_id)})

    prev_billing_state = account.billing_state
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
    # Canonical billing lifecycle_transition (payg_enabled → free_cap), §3.6.7 / §5.2.
    # Only on a real state change (idempotent redelivery safe) (Axis3-F04).
    if prev_billing_state != "free_cap":
        await audit.emit(
            session,
            admin_id=admin_id,
            actor_type=ActorType.platform,
            event_type="lifecycle_transition",
            payload={"from": prev_billing_state, "to": "free_cap", "trigger": "payment_method"},
        )
    # Legal §A3: the card is gone — action-tool connections go dormant immediately
    # (not just at day-7 dunning exhaustion; a detached card is an immediate loss).
    await dunning.set_dormant_for_action_tools(
        session, admin_id=admin_id, reason="payment_method_detached"
    )
    return "payment_method.detached"
