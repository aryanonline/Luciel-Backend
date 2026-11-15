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
                                      no longer holds (closure / card removal), and any
                                      paid invoice that is not a renewal (one-off,
                                      update, threshold, the $0 creation invoice),
                                      settle WITHOUT advancing the anchor or the
                                      watermarks.
  * invoice.payment_failed          → drive dunning none→retrying, email notification +
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

A subscription renewal reaches this module as the renewal invoice's ``invoice.paid``
(``billing_reason`` ``subscription_cycle``). Stripe has no
``customer.subscription.renewed`` event (the branch Axis8-F02 added for one never ran).
The counter itself rolls at the cycle end by time (``budget.current_period``, F030):
Stripe does not try to collect the renewal invoice until an hour after creating it, so
the period has already turned when this event lands. The renewal's ``invoice.paid``
then moves the stored anchor to the invoice's ``period_end`` and takes the new period's
end from Stripe, flushes the settled period's residual under that period's own
identifiers, resets the watermarks unless a counted turn already rolled them, clears
dunning and restores dormant connections. The ``customer.subscription.updated`` sent as
the period advances changes nothing: it fires on any change to a subscription and does
not mean anything was paid. Every other event type is acknowledged and ignored.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.billing import dunning, metering
from app.billing.metering import get_or_create_billing_account
from app.billing.stripe_gateway import (
    StripeEvent,
    add_months,
    get_stripe_gateway,
    next_period_end,
)
from app.models.audit import ActorType
from app.models.billing import BillingAccount, ProcessedStripeEvent
from app.models.connection import InstanceConnection
from app.notifications import notifier as notify

_log = logging.getLogger(__name__)

# The ``billing_reason`` of the invoice Stripe issues when a subscription "advanced into
# a new period" (Invoice object reference). The only paid invoice that ends a period.
_RENEWAL_BILLING_REASON = "subscription_cycle"


class _EventHandler(Protocol):
    """What ``handle_event`` calls for one event type; returns the action taken.

    Every handler takes the same keywords, so the dispatch is one table
    (``_HANDLERS``); a handler ignores the ones it does not need."""

    async def __call__(
        self,
        session: AsyncSession,
        *,
        admin_id: uuid.UUID,
        account: BillingAccount,
        event: StripeEvent,
        now: datetime,
    ) -> str: ...


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

    handler = _HANDLERS.get(event.type)
    if handler is None:
        action = "ignored"
    else:
        action = await handler(session, admin_id=admin_id, account=account, event=event, now=now)

    account.last_stripe_event_id = event.event_id
    await session.flush()
    return action


async def _on_invoice_paid(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    account: BillingAccount,
    event: StripeEvent,
    now: datetime,
) -> str:
    """invoice.paid → the NEXT period begins, dunning clears, dormant connections restore.

    Period is Stripe-billing-date anchored (§3.4.1b). The invoice's ``period_start`` /
    ``period_end`` describe the period that was JUST BILLED, so the new anchor is the
    invoice's ``period_end`` (which is the next period's start) — the old code wrote
    ``period_start`` back as the anchor, re-opening the period that had just been paid
    for (audit F005). When the invoice carries no period the anchor advances one cycle.
    The new period's end is Stripe's own (``_renewed_period_end``), never a month added
    to a clamped date: a Jan-31 anchor's Feb-28 period ends on Mar 31.

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

    Nor is any other paid invoice that is not the renewal. Stripe marks the renewal
    ``billing_reason`` ``subscription_cycle``; a one-off invoice (``manual``, no parent,
    ``period_start == period_end`` == the moment it was created), a subscription update
    and a billing threshold settle debt INSIDE the Stripe period. Reading their
    ``period_end`` as the next period's start began a new period mid-cycle, with a
    second free 50. They settle like the ended subscription's invoice. So does a
    subscription creation invoice, though only for safety. Once the bind had stored
    Stripe's period it moved nothing: its ``period_end`` is the subscription's start,
    the anchor stored then (``metering._anchor_to_subscription``). And Stripe issues
    none for a metered-only subscription in flexible billing mode, the default when a
    subscription is created on API version 2025-09-30.clover or later
    (``STRIPE_API_VERSION`` is later). An invoice without a ``billing_reason`` (a
    payload from before the field existed, a bare test event) is still read as the
    renewal.
    """
    billed_subscription = _invoice_subscription_id(event)
    if billed_subscription is not None and billed_subscription != account.stripe_subscription_id:
        return await _settle_without_period_change(
            session,
            admin_id=admin_id,
            account=account,
            reason="ended_subscription_settled",
            detail={"subscription_id": billed_subscription},
        )
    billing_reason = event.data.get("billing_reason")
    if isinstance(billing_reason, str) and billing_reason != _RENEWAL_BILLING_REASON:
        return await _settle_without_period_change(
            session,
            admin_id=admin_id,
            account=account,
            reason="not_a_renewal",
            detail={"billing_reason": billing_reason, "subscription_id": billed_subscription},
        )
    prev_start = account.period_start.astimezone(UTC) if account.period_start else None
    settled_from, new_start = _settled_period(account, event)
    # Never move the anchor backwards: a late-delivered invoice for a period the anchor
    # has already rolled past (current_period advances on its own) must not re-open it.
    # Such an invoice, or the same invoice delivered again as a separate event, leaves
    # the period exactly as it is, end included.
    if prev_start is not None and new_start <= prev_start:
        new_start, advanced = prev_start, False
    else:
        advanced = True
        account.period_start = new_start
        account.period_resets_at = await _renewed_period_end(
            account, start=new_start, settled_from=settled_from
        )

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

    # "period_advanced" only when the anchor moved: the owner's log renders it as "billing
    # period advanced", which a late or repeated invoice did not do.
    reset: dict[str, object] = (
        {"reset": "period_advanced"}
        if advanced
        else {"reset": "none", "reason": "period_already_current"}
    )
    await audit.emit(
        session,
        admin_id=admin_id,
        actor_type=ActorType.platform,
        event_type="invoice_paid",
        payload={
            **reset,
            "rollover": False,
            "ledger_retained": True,
            "period_start": new_start.date().isoformat(),
            "previous_period_start": prev_start.date().isoformat() if prev_start else None,
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


def _settled_period(
    account: BillingAccount, event: StripeEvent
) -> tuple[datetime | None, datetime]:
    """The period a paid invoice settled, as ``(start, end)``: ``end`` is where the next
    period starts, ``start`` is None when nothing says where the settled one began.

    A cycle invoice states both (``period_start`` / ``period_end``). An event without
    them falls back to the account's own period, then to one cycle on from its anchor.
    """
    ps = event.data.get("period_start")
    pe = event.data.get("period_end")
    start = datetime.fromtimestamp(ps, tz=UTC) if isinstance(ps, (int, float)) else None
    if isinstance(pe, (int, float)):
        return start, datetime.fromtimestamp(pe, tz=UTC)
    if start is not None:
        return start, add_months(start, 1)
    anchor = account.period_start.astimezone(UTC) if account.period_start else None
    if account.period_resets_at is not None:
        return anchor, account.period_resets_at.astimezone(UTC)
    if anchor is not None:
        return anchor, add_months(anchor, 1)
    return None, datetime.now(UTC)


async def _renewed_period_end(
    account: BillingAccount, *, start: datetime, settled_from: datetime | None
) -> datetime:
    """When the period that a paid cycle invoice opened at ``start`` ends.

    Stripe's subscription holds the answer (``items[].current_period_end``) and is asked
    first, as it is when the subscription is bound (``metering._anchor_to_subscription``).
    The answer counts only when it describes this period: an invoice paid after the next
    renewal finds the subscription a period further on. Otherwise the end follows from
    the settled period's own bounds, on Stripe's anchor day (``next_period_end``).
    ``add_months(start, 1)`` used to set it, which put the renewal after a Jan-31
    anchor's Feb-28 one on Mar 28 instead of Mar 31, so the counter rolled into a new
    period, with a second free 50, three days before Stripe's period ended.
    """
    if account.stripe_subscription_id is not None:
        try:
            bounds = await get_stripe_gateway().get_subscription_period(
                subscription_id=account.stripe_subscription_id
            )
        except Exception:  # noqa: BLE001 — the computed end below serves instead
            _log.exception("subscription period read failed admin_id=%s", account.admin_id)
            bounds = None
        if bounds is not None:
            current_start, current_end = bounds[0].astimezone(UTC), bounds[1].astimezone(UTC)
            if current_start == start and current_end > start:
                return current_end
    if settled_from is not None and settled_from < start:
        return next_period_end(settled_from, start)
    return add_months(start, 1)


def _invoice_subscription_id(event: StripeEvent) -> str | None:
    """The subscription an ``invoice.paid`` bills, or None when it names none.

    API 2025-03-31 "basil" moved ``invoice.subscription`` under
    ``invoice.parent.subscription_details.subscription``. A webhook payload has the shape
    of the API version set on its webhook endpoint in the Stripe Dashboard (the gateway's
    ``STRIPE_API_VERSION`` governs only our requests), so an endpoint on a version before
    basil still sends it at the top level (possibly expanded). Read either."""
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


async def _settle_without_period_change(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    account: BillingAccount,
    reason: str,
    detail: dict[str, object],
) -> str:
    """A paid invoice that does not end the period: the debt is settled, nothing else.

    Two kinds reach here. The final invoice of a subscription that is no longer the
    account's (``ended_subscription_settled``): closure and card removal both cancel the
    metered subscription with ``invoice_now`` after flushing the residual, so it
    collects blocks the watermarks already record as reported. And a paid invoice that
    is not the renewal (``not_a_renewal``, see ``_on_invoice_paid``). Either way the
    period the counter is keyed on has NOT ended: it keeps its anchor (rolling by whole
    cycles on its own, ``current_period``) and its watermarks, so a reopen or a re-added
    card continues it instead of starting a fresh free 50, and a mid-period payment
    never opens one. The debt is settled, so dunning clears and dormant tools return as
    for any payment.
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
            "reason": reason,
            **detail,
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
    now: datetime,
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
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    account: BillingAccount,
    event: StripeEvent,
    now: datetime,
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


# The whole dispatch: handle_event acts on these event types and acknowledges every other
# one as "ignored". A handler keyed on a type Stripe never sends can never run (Axis8-F02's
# customer.subscription.renewed), so tests/unit/test_stripe_event_types.py checks these
# keys against stripe-python's list of webhook event types. The Stripe Dashboard endpoint
# subscribes to exactly these (docs/operations/teardown-and-restore.md).
_HANDLERS: dict[str, _EventHandler] = {
    "invoice.paid": _on_invoice_paid,
    "invoice.payment_failed": _on_payment_failed,
    "payment_method.attached": _on_pm_attached,
    "payment_method.detached": _on_pm_detached,
}
HANDLED_EVENT_TYPES = frozenset(_HANDLERS)
