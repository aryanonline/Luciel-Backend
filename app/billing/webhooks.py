"""Stripe webhook event processing (Architecture §3.4.1b, CJ §6, Legal §A3).

Pure-ish handlers that mutate the tenant's BillingAccount + reset the budget
counter in response to verified Stripe events. The HTTP route
(app/api/v1/webhooks/stripe.py) verifies the signature FIRST (HARD CONSTRAINT #4),
then dispatches here. Idempotent on the Stripe event id (HARD CONSTRAINT, §3.4.1b):
a redelivered event is a no-op — Stripe may redeliver.

Events handled:
  * invoice.paid                    → advance the period anchor to the NEXT Stripe period
                                      (the counter is keyed by period, so it starts at
                                      zero by construction — NO rollover), clear dunning,
                                      restore dormant connections (Legal §A3 / §3.6.7).
                                      The settled period's ledger rows are KEPT: they are
                                      the evidence behind the invoice (audit F005/F042).
                                      The final invoice of a subscription the account
                                      no longer holds (closure / card removal) settles
                                      WITHOUT advancing the anchor or the watermarks.
  * invoice.payment_failed         → drive dunning none→retrying, email notification +
                                      lifecycle_notice_sent audit (Legal §A3 / §3.6.7).
                                      The attempt number is Stripe's own ``attempt_count``.
  * payment_method.attached         → flip billingState→payg_enabled + store display tuple,
                                      make the card the invoice default, clear any dunning
                                      (a new card IS the remedy, F012), restore dormant
                                      connections to their pre-dormant status (Legal §A3).
  * payment_method.detached         → flip billingState→free_cap + clear display tuple,
                                      tell the owner (email + escalation SMS when on
                                      file, lifecycle_notice_sent). NOTHING else changes
                                      (owner decision D1, 2026-09-14): connections keep
                                      their status; only the day-7 dunning reduction
                                      dormants action tools.
  * customer.subscription.renewed   → same as invoice.paid (§3.4.1b).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.billing import dunning, metering
from app.billing.metering import get_or_create_billing_account
from app.billing.stripe_gateway import StripeEvent, add_months, get_stripe_gateway
from app.models.audit import ActorType
from app.models.billing import BillingAccount, ProcessedStripeEvent
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
        action = await _on_payment_failed(
            session, admin_id=admin_id, account=account, event=event, now=now
        )
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
    """invoice.paid → the NEXT period begins, dunning clears, dormant connections restore.

    Period is Stripe-billing-date anchored (§3.4.1b). The invoice's ``period_start`` /
    ``period_end`` describe the period that was JUST BILLED, so the new anchor is the
    invoice's ``period_end`` (which is the next period's start) — the old code wrote
    ``period_start`` back as the anchor, re-opening the period that had just been paid
    for (audit F005). When the invoice carries no period the anchor advances one cycle.

    The counter is keyed by period start, so the new period reads zero without deleting
    anything: the settled period's ``conversation_budgets`` / ``counted_sessions`` rows
    are the evidence behind the invoice and are KEPT for disputes (F042). Deleting them
    also made the fire-once guard forget every session, so a conversation that straddled
    the boundary was counted again on its next turn (F029).

    An invoice for a subscription the account no longer holds is NOT a period boundary
    (round 7 money review, finding 0): it is the final invoice of a subscription that
    closure (or a card removal) cancelled with ``invoice_now``. Its ``period_end`` is
    the cancellation instant, and treating that as the next period's start handed a
    reopened account a second free 50 inside the Stripe period it had already used,
    with the watermarks zeroed under it. That payment clears dunning like any other,
    but the anchor and the watermarks stay where they are.
    """
    billed_subscription = _invoice_subscription_id(event)
    if billed_subscription is not None and billed_subscription != account.stripe_subscription_id:
        return await _on_ended_subscription_paid(
            session, admin_id=admin_id, account=account, subscription_id=billed_subscription
        )
    prev_start = account.period_start
    ps = event.data.get("period_start")
    pe = event.data.get("period_end")
    if isinstance(pe, (int, float)):
        new_start = datetime.fromtimestamp(pe, tz=UTC)
    elif isinstance(ps, (int, float)):
        new_start = add_months(datetime.fromtimestamp(ps, tz=UTC), 1)
    elif account.period_resets_at is not None:
        new_start = account.period_resets_at.astimezone(UTC)
    elif account.period_start is not None:
        new_start = add_months(account.period_start.astimezone(UTC), 1)
    else:
        new_start = datetime.now(UTC)
    # Never move the anchor backwards: a late-delivered invoice for a period the anchor
    # has already rolled past (current_period advances on its own) must not re-open it.
    if prev_start is not None and new_start <= prev_start.astimezone(UTC):
        new_start = prev_start.astimezone(UTC)
    account.period_start = new_start
    account.period_resets_at = add_months(new_start, 1)

    # The usage watermarks reset ONLY when they still describe the period this invoice
    # settled. Two cases must leave them alone: the $0 invoice Stripe issues at
    # subscription creation (its period_end is the anchor that was just set — nothing
    # was settled), and an invoice that lands after a counted turn already rolled the
    # watermarks into the new period (metering._roll_accrual_period). Zeroing in either
    # case re-posts the same block later under an identifier Stripe only dedupes for
    # 24 hours — a double bill. Any residual of the settled period is flushed first,
    # under that period's own identifiers, so it lands on the next invoice.
    new_period = new_start.date()
    if account.accrual_period_start is None or account.accrual_period_start < new_period:
        try:
            await metering.report_accrued_usage(session, admin_id=admin_id)
        except Exception:  # noqa: BLE001 — the watermark stays; the reconcile sweep retries
            _log.exception("settled-period residual flush failed admin_id=%s", admin_id)
        else:
            account.accrued_blocks = 0
            account.last_reported_blocks = 0
            account.accrual_period_start = new_period

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
        payload={
            "reset": "period_advanced",
            "rollover": False,
            "ledger_retained": True,
            "period_start": new_start.date().isoformat(),
            "previous_period_start": (
                prev_start.astimezone(UTC).date().isoformat() if prev_start else None
            ),
        },
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
    await _restore_dormant_connections(session, admin_id=admin_id, reason="invoice_paid")
    return "invoice_paid"


def _invoice_subscription_id(event: StripeEvent) -> str | None:
    """The subscription an ``invoice.paid`` bills, or None when it names none.

    API 2025-03-31 "basil" (the platform pin) moved ``invoice.subscription`` under
    ``invoice.parent.subscription_details.subscription``; an older-shaped payload
    still carries it at the top level (possibly expanded). Read either."""
    if event.type != "invoice.paid":
        return None
    data = event.data
    sub = data.get("subscription")
    if isinstance(sub, dict):
        sub = sub.get("id")
    if isinstance(sub, str) and sub:
        return sub
    parent = data.get("parent")
    details = parent.get("subscription_details") if isinstance(parent, dict) else None
    nested = details.get("subscription") if isinstance(details, dict) else None
    if isinstance(nested, dict):
        nested = nested.get("id")
    return nested if isinstance(nested, str) and nested else None


async def _on_ended_subscription_paid(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    account: BillingAccount,
    subscription_id: str,
) -> str:
    """The final invoice of a subscription that is no longer the account's was paid.

    Closure and card removal both cancel the metered subscription with ``invoice_now``
    after flushing the residual, so this invoice collects blocks the watermarks already
    record as reported. The period the counter is keyed on has NOT ended: it keeps its
    anchor (rolling by whole cycles on its own, ``current_period``) and its watermarks,
    so a reopen or a re-added card continues it instead of starting a fresh free 50.
    The debt is settled, so dunning clears and dormant tools return as for any payment.
    """
    period_start = account.period_start
    prev_dunning = account.dunning_state
    account.dunning_state = dunning.on_payment_succeeded()
    account.dunning_started_at = None
    await session.flush()
    anchor = period_start.astimezone(UTC).date().isoformat() if period_start else None
    await audit.emit(
        session,
        admin_id=admin_id,
        actor_type=ActorType.platform,
        event_type="invoice_paid",
        payload={
            "reset": "none",
            "reason": "ended_subscription_settled",
            "subscription_id": subscription_id,
            "rollover": False,
            "ledger_retained": True,
            "period_start": anchor,
            "previous_period_start": anchor,
        },
    )
    if prev_dunning != account.dunning_state:
        await audit.emit(
            session,
            admin_id=admin_id,
            actor_type=ActorType.platform,
            event_type="dunning_state_changed",
            payload={"from": prev_dunning, "to": account.dunning_state, "reason": "invoice_paid"},
        )
    await _restore_dormant_connections(session, admin_id=admin_id, reason="invoice_paid")
    return "invoice_paid"


def _attempt_number(event: StripeEvent, account: BillingAccount, now: datetime) -> int:
    raw = event.data.get("attempt_count")
    if isinstance(raw, (int, float)) and int(raw) >= 1:
        return int(raw)
    # Legacy fallback (an event without the field): position in the CJ §6 schedule.
    if account.dunning_started_at is None:
        return 1
    elapsed_days = (now - account.dunning_started_at).days
    return sum(1 for d in dunning.RETRY_DAYS if elapsed_days >= d) or 1


async def _on_payment_failed(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    account: BillingAccount,
    event: StripeEvent,
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

    # The attempt number is Stripe's own ``attempt_count`` on the invoice: it is what
    # the email tells the owner ("attempt 2 of 4"), and deriving it from elapsed days
    # (audit F027) said "attempt 1" for every retry inside the first day and skipped
    # numbers when Stripe's smart retries did not land exactly on days 1/3/5/7.
    attempt_number = _attempt_number(event, account, now)

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
    # The event's ``id`` is the payment-method id; Stripe holds the card, we hold the
    # pointer — and only long enough to make it the invoice default.
    pm_id = event.data.get("id")
    await apply_card_on_file(
        session,
        admin_id=admin_id,
        account=account,
        payment_method_id=pm_id if isinstance(pm_id, str) and pm_id else None,
        source="payment_method.attached",
    )
    return "payment_method.attached"


async def apply_card_on_file(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    account: BillingAccount,
    payment_method_id: str | None,
    source: str,
) -> None:
    """The ONE transition a card on file causes (§3.4.1b, Vision §7) — idempotent.

    payg_enabled; the card made the invoice default when its id is known (F006);
    dunning cleared (a new card is the remedy, F012); dormant connections restored
    (Legal §A3); audited. Applied by the ``payment_method.attached`` webhook and —
    round 7 WP-1 #5 — by the billing read when Stripe already reports the card and the
    webhook has not yet been delivered, so "card saved" and "no card on file" never
    stand side by side on the dashboard. Whichever path runs second finds nothing
    left to change (no second lifecycle_transition, no second dunning change).

    The caller stores the display tuple first. ``source`` names the path in the
    audit trail; ``payment_method_id`` is None when the path has no pointer (the read
    only sees the display tuple) — the default is then left for the webhook to set.
    """
    prev_billing_state = account.billing_state
    account.billing_state = "payg_enabled"  # adding a card flips behavior >50 (Vision §7)

    # A card attached through Checkout in setup mode is NOT automatically the customer's
    # invoice default (audit F006): without this, the first metered invoice had no
    # payment method to charge and went straight to payment_failed.
    default_set: bool | None = None
    if payment_method_id and account.stripe_customer_id:
        try:
            default_set = await get_stripe_gateway().set_default_payment_method(
                customer_id=account.stripe_customer_id, payment_method_id=payment_method_id
            )
        except Exception:  # noqa: BLE001 — never strand the attach event on a Stripe blip
            _log.exception("default payment method set failed admin_id=%s", admin_id)
            default_set = False

    # A new card is the remedy for a failing one: leaving dunning in place (F012) kept
    # the account in ``reduced`` — capped at 50 with tools dormant — with a valid card
    # on file until an invoice.paid happened to arrive. Stripe retries the open invoice
    # against the new default on its own schedule.
    prev_dunning = account.dunning_state
    if prev_dunning != dunning.NONE:
        account.dunning_state = dunning.on_payment_succeeded()
        account.dunning_started_at = None
    await session.flush()

    await audit.emit(
        session,
        admin_id=admin_id,
        actor_type=ActorType.platform,
        event_type="payment_method_added",
        payload={
            "brand": account.pm_brand,
            "last4": account.pm_last4,
            "invoice_default_set": default_set,
            "source": source,
        },
    )
    if prev_dunning != account.dunning_state:
        await audit.emit(
            session,
            admin_id=admin_id,
            actor_type=ActorType.platform,
            event_type="dunning_state_changed",
            payload={
                "from": prev_dunning,
                "to": account.dunning_state,
                "reason": "payment_method_attached",
            },
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
    await _restore_dormant_connections(session, admin_id=admin_id, reason="payment_method")


async def _restore_dormant_connections(
    session: AsyncSession, *, admin_id: uuid.UUID, reason: str = "payment"
) -> int:
    """Return dormant connections to the status they held BEFORE going dormant
    (Legal §A3 / §3.6.7).

    ``set_dormant_for_action_tools`` stashes each row's prior status and detail (the
    same stash columns the delete/restore pair uses, migration 0049). A connection
    that was ``connected`` comes back ``connected`` — a card re-add is a billing
    event, not a credential event, and blanket ``unconfigured`` (audit F069) told the
    owner to reconnect a calendar whose OAuth grant never changed. Pre-0049 dormant
    rows carry no stash and fall back to ``unconfigured`` so the health worker
    re-probes them.

    Returns the number of rows transitioned."""
    rows = (
        (
            await session.execute(
                select(InstanceConnection).where(
                    InstanceConnection.admin_id == admin_id,
                    InstanceConnection.status == "dormant",
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.status = row.status_stashed or "unconfigured"
        row.status_detail = (
            row.status_detail_stashed if row.status_stashed else "restored_after_payment"
        )
        row.status_stashed = None
        row.status_detail_stashed = None
    restored = len(rows)
    if restored > 0:
        await session.flush()
        await audit.emit(
            session,
            admin_id=admin_id,
            actor_type=ActorType.platform,
            event_type="connection_status_changed",
            payload={
                "reason": f"dormant_connections_restored_on_{reason}",
                "count": restored,
                "new_status": "prior_status",
            },
        )
    return restored


async def _on_pm_detached(
    session: AsyncSession, *, admin_id: uuid.UUID, account: BillingAccount
) -> str:
    """payment_method.detached → free_cap + clear the display tuple (§3.4.1b).

    Owner decision D1 (2026-09-14): a card removal — here or through the admin's
    DELETE — changes NOTHING but the 50-conversation cap. No connection goes
    dormant on this path; that is the day-7 dunning reduction's consequence alone.
    """
    # Bill what they already ran BEFORE the account stops looking billable: once
    # billing_state is free_cap, build_budget reports zero blocks and report_usage
    # refuses the account, so any accrued overage would simply vanish (P1-13).
    try:
        await metering.report_accrued_usage(session, admin_id=admin_id)
    except Exception:  # noqa: BLE001 — a Stripe blip must not strand the detach event
        # The watermark is unmoved, so the reconcile sweep picks this tenant up again.
        _log.exception("accrued usage flush failed at detach admin_id=%s", admin_id)

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
    # D1: no dormancy here. The cap is the whole consequence of a card removal — and
    # the owner is told so (email + escalation SMS when on file; Legal §A3 / §3.6.7,
    # round 7 WP-1 #3c). A Stripe-side detach is the one the owner may not have done.
    await notify.notify_payment_method_removed(session, admin_id=admin_id, reason="stripe_detached")
    return "payment_method.detached"
