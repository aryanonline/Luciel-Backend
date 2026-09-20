"""Conversation counter + Stripe overage reporting (DOCTRINE ANCHOR — §3.4.1b).

Counter: once per session (first model call OR human-controlled-before-model),
idempotent on session id, synchronous write-through to Postgres (source of truth)
+ Redis (hot cache); reset on Stripe invoice.paid; no rollover. PAYG $39/100 above
free 50, rounded up per 100-block, monthly arrears via Stripe metered usage.

This module builds the metering layer ON TOP of the Arc 14 counter in
``app/runtime/budget.py`` (the single source of the count — NOT duplicated here):
it reads the counted conversations and computes
  * ``billed_conversations`` — conversations above the free 50 (the wire field
    ``billedThisPeriod``, a COUNT — frozen billing.ts ``billedThisPeriod`` is
    z.number().int().nonnegative()), and
  * ``billed_blocks`` / ``billed_cents`` — the $39-per-100-block, rounded-UP
    money used for Stripe metered-usage reporting (the locked numbers: 50→$0,
    51→$39, 150→$39, 151→$78 — Vision §7, §3.4.1b, BUILD_SPEC ~193).

Idempotent reporting (HARD CONSTRAINT #3, §3.4.1b): metered usage is reported to
Stripe with an idempotency key per (period, quantity) so a retry / webhook
redelivery never double-bills.

Storage honesty (Architecture §3.4.1b): the counter is written synchronously to
Postgres (the source of truth) in budget.py; a Redis hot cache in front of the
counter is the ratified target optimization but is NOT yet wired into the budget
path (no Redis import in the billing/budget modules today).
"""

from __future__ import annotations

import logging
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.billing import dunning
from app.billing.stripe_gateway import StripeGateway, get_stripe_gateway
from app.config import get_settings
from app.models.audit import ActorType, AdminAuditLog
from app.models.billing import BillingAccount
from app.models.budget import ConversationBudget, CountedSession
from app.runtime.budget import current_period, period_start_for

_log = logging.getLogger(__name__)


# --- pure metering math (the locked-number core) ---------------------------- #
def billed_conversations(counted: int, free_allowance: int) -> int:
    """Conversations above the free allowance this period (wire ``billedThisPeriod``).

    A COUNT, not money: 50→0, 51→1, 240→190 (matches the frozen payg seed). Never
    negative (§3.4.1b)."""
    return max(0, counted - free_allowance)


def billed_blocks(counted: int, free_allowance: int, block_size: int) -> int:
    """PAYG blocks billed: ceil(over / 100), rounded UP per 100-block (Vision §7).

    50→0 blocks, 51→1, 100 over→1, 101 over→2. This is the locked rounding rule."""
    over = billed_conversations(counted, free_allowance)
    if over == 0:
        return 0
    return math.ceil(over / block_size)


def billed_cents(
    counted: int, free_allowance: int, block_size: int, price_per_block_cents: int
) -> int:
    """Amount in cents (CAD) for the period: blocks × $39 CAD (the locked numbers).

    50→$0, 51→$39 (3900¢), 150→$39, 151→$78 (7800¢) — exhaustively tested at the
    100-block boundaries (Vision §7, §3.4.1b, BUILD_SPEC ~193)."""
    return billed_blocks(counted, free_allowance, block_size) * price_per_block_cents


# --- assembled budget state (drives the BillingInfo wire model) ------------- #
@dataclass
class MeteredBudget:
    """The fully-resolved budget for the BillingInfo read (frozen billing.ts)."""

    billing_state: str  # free_cap | payg_enabled
    dunning_state: str  # none | retrying | reduced
    conversations_this_period: int
    free_allowance: int  # serializes literally 50 (z.literal(50))
    billed_this_period: int  # billed-conversation COUNT above free
    period_resets_at: datetime
    at_cap: bool
    # PAYG ~80%-through-current-100-block heads-up (§3.4.1b / Vision §7). Nudge, not a cap.
    near_next_block: bool

    # Money side (for Stripe reporting; NOT on the wire Budget shape).
    billed_blocks: int
    billed_cents: int

    # Ledger truth (round 6 WP-I, audit F025). ``billable`` is whether NEW usage past
    # the free 50 is served and billed right now (a live card, dunning not reduced);
    # the two block counts are this period's watermarks — blocks accrued against the
    # tenant and blocks already reported to Stripe — so a cardless or dunned account
    # still shows the blocks it ran while it could, instead of a zero that reads as
    # "nothing was ever billed".
    billable: bool = True
    accrued_blocks: int = 0
    reported_blocks: int = 0


async def get_or_create_billing_account(
    session: AsyncSession, *, admin_id: uuid.UUID
) -> BillingAccount:
    """The tenant's single billing account, created lazily in free_cap (§3.4.1b)."""
    row = (
        await session.execute(select(BillingAccount).where(BillingAccount.admin_id == admin_id))
    ).scalar_one_or_none()
    if row is None:
        row = BillingAccount(
            id=uuid.uuid4(),
            admin_id=admin_id,
            billing_state="free_cap",
            dunning_state="none",
        )
        session.add(row)
        await session.flush()
    return row


async def has_live_card(session: AsyncSession, *, admin_id: uuid.UUID) -> bool:
    """Whether the tenant can be served past the free allowance (Vision §7 PAYG).

    True only when a card is on file AND dunning has not reverted the account to
    free-cap behaviour (Legal §A3). This is the value every serving path must pass as
    ``has_payment_method`` — passing a literal False caps a paying customer at 50,
    inverting the PAYG promise (§12.9).
    """
    account = await get_or_create_billing_account(session, admin_id=admin_id)
    return account.billing_state == "payg_enabled" and not dunning.behaves_as_free_cap(
        account.dunning_state
    )


async def build_budget(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    now: datetime | None = None,
) -> MeteredBudget:
    """Assemble the budget for the BillingInfo read (frozen billing.ts).

    at_cap is DATA, not an error (objective / §3.4.1b): true when the free 50 is
    consumed AND the account is effectively cardless — either no payment method
    (free_cap) OR dunning has reached ``reduced`` (Legal §A3 reverts to free-cap
    behavior). With a live card (payg_enabled, dunning none/retrying) above 50 we
    are NOT at_cap and the overage is billed."""
    now = now or datetime.now(UTC)
    settings = get_settings()
    free = settings.free_allowance  # literally 50 (Vision §7)

    account = await get_or_create_billing_account(session, admin_id=admin_id)

    # Read the counter from the Arc 14 source of truth — do NOT recount (§3.4.1b).
    # Same anchor the counter was written under, so the read cannot miss the row.
    period, resets_at = current_period(account, now)
    budget_row = (
        await session.execute(
            select(ConversationBudget).where(
                ConversationBudget.admin_id == admin_id,
                ConversationBudget.instance_id == instance_id,
                ConversationBudget.billing_period_start == period,
            )
        )
    ).scalar_one_or_none()
    counted = budget_row.counted_conversations if budget_row is not None else 0

    # Effective card: payg only if a card is on file AND dunning hasn't reduced us
    # to free-cap behavior (Legal §A3). reduced = behave as if cardless.
    live_card = account.billing_state == "payg_enabled" and not dunning.behaves_as_free_cap(
        account.dunning_state
    )

    at_cap = counted >= free and not live_card  # at_cap is data (§3.4.1b)

    # PAYG heads-up: only for a live-card account above the free allowance, true when
    # the account is >=80% through its CURRENT billed 100-block (position WITHIN the
    # block, not the whole period). e.g. over=80 -> 80/100 -> True; over=100 (a fresh
    # block just started) -> position 0 -> False. Honest early warning before a new
    # block is billed; never a cap.
    near_next_block = False
    if live_card and counted > free:
        position_in_block = (counted - free) % settings.payg_block_size
        near_next_block = position_in_block >= int(settings.payg_block_size * 0.8)

    # Ledger truth (round 6 WP-I, F025): the watermarks the meter actually posted
    # against this period. With a live card the count above free is what is billed;
    # without one (card removed, dunning reduced) the blocks that ran while the card
    # WAS live are still owed and still shown — bounded by what the ledger accrued,
    # so a capped tenant is never shown a charge that was never metered.
    same_period = account.accrual_period_start == period
    accrued_blocks = account.accrued_blocks if same_period else 0
    reported_blocks = account.last_reported_blocks if same_period else 0
    if live_card:
        billed_count = billed_conversations(counted, free)
    else:
        billed_count = min(max(counted - free, 0), accrued_blocks * settings.payg_block_size)
    blocks = billed_blocks(counted, free, settings.payg_block_size) if live_card else 0
    cents = (
        billed_cents(counted, free, settings.payg_block_size, settings.payg_price_per_block_cents)
        if live_card
        else 0
    )

    # periodResetsAt: the end of the period the counter is keyed on — the Stripe
    # period once one exists (rolled forward by whole cycles if an invoice.paid never
    # came), the calendar month before that. Always the same anchor as ``period``.
    return MeteredBudget(
        billing_state=account.billing_state,
        dunning_state=account.dunning_state,
        conversations_this_period=counted,
        free_allowance=free,
        billed_this_period=billed_count,
        period_resets_at=resets_at,
        at_cap=at_cap,
        near_next_block=near_next_block,
        billed_blocks=blocks,
        billed_cents=cents,
        billable=live_card,
        accrued_blocks=accrued_blocks,
        reported_blocks=reported_blocks,
    )


# --- idempotent Stripe metered-usage reporting (HARD CONSTRAINT #3) --------- #
def meter_event_identifier(
    *, customer_id: str, period_start: date, from_block: int, to_block: int
) -> str:
    """Stripe dedup identifier for the meter event covering blocks ``from``→``to``.

    Meter events are additive, so the identifier is the whole of the double-report
    defence: a retry after a timeout must carry the SAME identifier or its value is
    added to the meter a second time. Naming the watermark span rather than a bare
    quantity is what makes that automatic — a retry recomputes the identical span,
    while genuinely new usage always starts where the last accepted event ended.

    ``period_start`` must be the same anchor the counter is keyed on
    (``period_start_for``); if the identifier names a different period than the blocks
    were counted under, a genuinely new block reuses a spent identifier and is silently
    dropped (P0-4).
    """
    return f"usage:{customer_id}:{period_start.isoformat()}:{from_block}-{to_block}"


async def report_usage_after_count(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    now: datetime | None = None,
) -> int:
    """Reconcile this tenant's metered usage against Stripe after a conversation counted.

    Called from the runtime immediately after the fire-once counter increments — this is
    what actually connects the (already correct) metering math to Stripe.

    This used to fire only on the exact boundary conversation, so a Stripe blip at
    conversation 151 lost block 2 permanently: there was no record that a report was
    owed and nothing ever came back for it. Reporting is now driven by the persisted
    watermark instead, which makes every turn a retry opportunity. A Stripe failure
    must never fail the lead's turn, so it is still swallowed here.
    """
    try:
        return await report_usage(session, admin_id=admin_id, instance_id=instance_id, now=now)
    except Exception:  # noqa: BLE001 — never let billing break a conversation
        # Swallowed for the lead's turn, NOT for the operator (audit F038): the watermark
        # is unmoved so the next turn / reconcile sweep retries, and this line is what
        # the sweep log group's error alarm sees.
        _log.exception("usage report failed admin_id=%s instance_id=%s", admin_id, instance_id)
        return 0


async def report_usage(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    now: datetime | None = None,
    gateway: StripeGateway | None = None,
) -> int:
    """Report this period's newly accrued blocks to the Stripe meter, idempotently.

    Returns the number of blocks newly reported — the delta past the watermark, not the
    period total, because meter events are additive. 0 when Stripe is already up to
    date, there is no live subscription, or nothing is billable. Each event carries a
    deterministic identifier naming its watermark span, so a retry is deduped by Stripe
    (HARD CONSTRAINT #3)."""
    gateway = gateway or get_stripe_gateway()
    now = now or datetime.now(UTC)
    account = await get_or_create_billing_account(session, admin_id=admin_id)

    period = period_start_for(account, now)
    had_subscription = account.stripe_subscription_id is not None
    if not await _roll_accrual_period(session, account=account, period=period, gateway=gateway):
        # An older period's residual is still unposted and there is no subscription to
        # post it to. The watermarks are kept as they are (round 7 WP-1 #4) — nothing
        # accrues against the new period until the old one is settled, or the two
        # would share one watermark and the smaller be lost.
        await _alarm_meter_unbound(session, account=account, period=account.accrual_period_start)
        return 0
    if not had_subscription and account.stripe_subscription_id is not None:
        # The roll bound the subscription to settle the residual: billing has begun, so
        # the counter moves to the Stripe cycle exactly as the lazy bind below does.
        period = await _anchor_to_subscription(
            session,
            account=account,
            instance_id=instance_id,
            sub_id=account.stripe_subscription_id,
            gateway=gateway,
            now=now,
        )

    budget = await build_budget(session, admin_id=admin_id, instance_id=instance_id, now=now)
    # The accrual is the period high-water mark, not the current reading: build_budget
    # returns zero blocks once the card is gone, and blocks already run are still owed.
    if budget.billed_blocks > account.accrued_blocks:
        account.accrued_blocks = budget.billed_blocks
        await session.flush()

    # PAYG is what makes usage billable: a free_cap tenant is blocked at the cap
    # instead (Vision §7), so there is nothing to meter.
    if account.billing_state != "payg_enabled":
        return 0

    quantity = account.accrued_blocks
    if quantity <= account.last_reported_blocks:
        return 0  # Stripe already has this total

    if account.stripe_subscription_id is None:
        # First billable block for this tenant: the metered subscription has to exist
        # before usage can attach to it. Bound lazily here rather than at card-attach
        # so a tenant who never exceeds the free 50 never gets a subscription.
        if account.stripe_customer_id is None:
            return 0
        sub_id = await gateway.ensure_metered_subscription(customer_id=account.stripe_customer_id)
        if sub_id is None:
            # No metered price bound → report nothing rather than mis-bill. The blocks
            # stay accrued on the watermark (never zeroed) and, outside the mock posture,
            # this is an operator-visible failure, not a quiet zero (round 7 WP-1 #4).
            await _alarm_meter_unbound(session, account=account, period=period)
            return 0
        account.stripe_subscription_id = sub_id
        await session.flush()
        # Billing has begun: from here the free 50 is "per Stripe billing period", so
        # the counter's anchor moves from the calendar month to the subscription's
        # cycle and the rows counted so far move with it (audit F004/F030).
        period = await _anchor_to_subscription(
            session,
            account=account,
            instance_id=instance_id,
            sub_id=sub_id,
            gateway=gateway,
            now=now,
        )

    return await _post_usage(
        session, account=account, quantity=quantity, gateway=gateway, period=period
    )


async def _anchor_to_subscription(
    session: AsyncSession,
    *,
    account: BillingAccount,
    instance_id: uuid.UUID,
    sub_id: str,
    gateway: StripeGateway,
    now: datetime | None = None,
) -> date:
    """Re-key the tenant's period from the calendar fallback to the Stripe cycle.

    The subscription's current period is the truth of when the free allowance resets
    (§3.4.1b). The conversations already counted this calendar month belong to the
    cycle that just opened — they are the reason it opened — so the counter row and
    the fire-once ledger rows keyed on the old anchor move to the new one rather than
    resetting to zero (which would hand out a second free 50) or being stranded under
    a key nothing reads again (which would freeze the counter).

    ``now`` is the instant the caller keyed the counter on. The old anchor must be
    read at that same instant: a residual settled at a period roll (round 7 WP-1 #4)
    would otherwise re-key the rows of the period that just closed into the one that
    just opened, and count them twice.

    Returns the period the counter is now keyed on."""
    old_period = period_start_for(account, now)
    try:
        bounds = await gateway.get_subscription_period(subscription_id=sub_id)
    except Exception:  # noqa: BLE001 — the calendar anchor keeps working until the next try
        _log.exception("subscription period read failed admin_id=%s", account.admin_id)
        return old_period
    if bounds is None:
        return old_period
    start, end = bounds
    account.period_start = start
    account.period_resets_at = end
    new_period = start.astimezone(UTC).date()
    if new_period == old_period:
        await session.flush()
        return new_period
    await _rekey_period(session, account=account, old=old_period, new=new_period)
    account.accrual_period_start = new_period
    await session.flush()
    await audit.emit(
        session,
        admin_id=account.admin_id,
        instance_id=instance_id,
        actor_type=ActorType.platform,
        event_type="billing_period_anchored",
        payload={
            "from_period_start": old_period.isoformat(),
            "to_period_start": new_period.isoformat(),
            "period_resets_at": end.isoformat(),
            "subscription_id": sub_id,
        },
    )
    return new_period


async def _rekey_period(
    session: AsyncSession, *, account: BillingAccount, old: date, new: date
) -> None:
    """Move this tenant's counter + fire-once rows from period ``old`` to ``new``.

    A counter row can already exist under ``new`` (a webhook raced ahead); then the
    counts merge into it and the old row goes, so ``uq_budget_period`` is never
    violated and no conversation is counted twice or lost."""
    admin_id = account.admin_id
    old_rows = (
        (
            await session.execute(
                select(ConversationBudget).where(
                    ConversationBudget.admin_id == admin_id,
                    ConversationBudget.billing_period_start == old,
                )
            )
        )
        .scalars()
        .all()
    )
    for row in old_rows:
        target = (
            await session.execute(
                select(ConversationBudget).where(
                    ConversationBudget.admin_id == admin_id,
                    ConversationBudget.instance_id == row.instance_id,
                    ConversationBudget.billing_period_start == new,
                )
            )
        ).scalar_one_or_none()
        if target is None:
            row.billing_period_start = new
        else:
            target.counted_conversations += row.counted_conversations
            target.counted_session_ids = list(target.counted_session_ids) + list(
                row.counted_session_ids
            )
            await session.execute(delete(ConversationBudget).where(ConversationBudget.id == row.id))
    await session.execute(
        update(CountedSession)
        .where(
            CountedSession.admin_id == admin_id,
            CountedSession.billing_period_start == old,
        )
        .values(billing_period_start=new)
    )
    await session.flush()


async def _roll_accrual_period(
    session: AsyncSession,
    *,
    account: BillingAccount,
    period: date,
    gateway: StripeGateway,
) -> bool:
    """Bring the usage watermarks into the period the counter is keyed on.

    The counter rolls on its own (``current_period``), but the watermarks belong to
    whichever period they were accrued in. If a period ended without an
    ``invoice.paid`` — a failed invoice, a delayed or lost webhook — the old
    watermarks used to persist, so the new period's first block was silently
    withheld until its total exceeded the previous period's (audit F030/F004).

    Any residual the old period still owes is flushed under the OLD period's
    identifiers first; only then do the watermarks reset for the new period. A
    failing flush raises, leaving everything as it was for the next retry.

    A residual with NO subscription to post to — blocks accrued while the metered
    price was unbound (round 7 WP-1 #4) — is never zeroed: that voided usage the
    tenant ran with a live card. If the account can still be billed the subscription
    is bound here and the residual posted under its own period; otherwise the
    watermarks are left exactly as they were and False is returned, meaning the
    period did NOT roll and nothing may accrue against the new one yet.

    Returns whether the watermarks now describe ``period``."""
    if account.accrual_period_start is None:
        account.accrual_period_start = period
        await session.flush()
        return True
    if account.accrual_period_start == period:
        return True
    if account.accrued_blocks > account.last_reported_blocks:
        if account.stripe_subscription_id is None and (
            account.billing_state == "payg_enabled" and account.stripe_customer_id
        ):
            sub_id = await gateway.ensure_metered_subscription(
                customer_id=account.stripe_customer_id
            )
            if sub_id is not None:
                account.stripe_subscription_id = sub_id
                await session.flush()
        if account.stripe_subscription_id is None:
            return False
        await _post_usage(
            session,
            account=account,
            quantity=account.accrued_blocks,
            gateway=gateway,
            period=account.accrual_period_start,
        )
    account.accrued_blocks = 0
    account.last_reported_blocks = 0
    account.accrual_period_start = period
    await session.flush()
    return True


async def _alarm_meter_unbound(
    session: AsyncSession, *, account: BillingAccount, period: date | None
) -> None:
    """A live-card tenant has PAYG blocks nobody can report (round 7 WP-1 #4).

    ``ensure_metered_subscription`` returns None when ``STRIPE_PAYG_PRICE_ID`` is empty.
    In the mock posture that is the designed state (nothing is billed, CI/local) and
    stays silent. In the test/live posture it is a deployment gap that was returning a
    quiet 0 while ``accrued_blocks`` kept climbing: an ERROR line for the alarm, one
    ``billing_meter_unbound`` audit event per (tenant, accrual period) for the record,
    and ``integration_status.degraded_bindings`` names ``stripe_payg_price:unset``.
    Only fires for an account that is actually billable (a live card, a customer)."""
    settings = get_settings()
    if settings.stripe_integration_posture == "mock":
        return
    if account.billing_state != "payg_enabled" or account.stripe_customer_id is None:
        return
    unreported = account.accrued_blocks - account.last_reported_blocks
    period_key = period.isoformat() if period is not None else None
    _log.error(
        "PAYG meter unbound: stripe_payg_price_id is empty while admin_id=%s has a live "
        "card; %d accrued block(s) for period %s cannot be reported to Stripe",
        account.admin_id,
        unreported,
        period_key,
    )
    already = (
        await session.execute(
            select(AdminAuditLog.event_id)
            .where(
                AdminAuditLog.admin_id == account.admin_id,
                AdminAuditLog.event_type == "billing_meter_unbound",
                AdminAuditLog.event_payload["period_start"].astext == period_key,
            )
            .limit(1)
        )
    ).first()
    if already is not None:
        return
    await audit.emit(
        session,
        admin_id=account.admin_id,
        actor_type=ActorType.platform,
        event_type="billing_meter_unbound",
        payload={
            "period_start": period_key,
            "unreported_blocks": unreported,
            "binding": "stripe_payg_price:unset",
        },
    )


async def _post_usage(
    session: AsyncSession,
    *,
    account: BillingAccount,
    quantity: int,
    gateway: StripeGateway,
    period: date,
) -> int:
    """Post the blocks accrued since the watermark as a meter event, then advance it.

    Billing Meters are ADDITIVE — there is no "set" — so what goes to Stripe is the
    DELTA ``quantity - last_reported_blocks``, not the period total. Posting the total
    against an additive meter would invoice 1+2+3 = 6 blocks for 3 blocks of usage.

    The watermark moves only after Stripe accepts, so a raising post leaves the tenant
    looking behind and the next turn (or the reconcile sweep) retries it. That retry
    recomputes the same identifier, so Stripe dedupes it instead of adding the value
    twice: together the two rules give exactly-once billing per block.

    Returns the delta actually reported.
    """
    if account.stripe_customer_id is None:
        # A meter event is keyed on the customer, so without one there is nowhere to
        # post. Leave the watermark behind rather than pretending it was billed.
        return 0
    from_block = account.last_reported_blocks
    delta = quantity - from_block
    if delta <= 0:
        return 0
    await gateway.report_meter_event(
        customer_id=account.stripe_customer_id,
        value=delta,
        identifier=meter_event_identifier(
            customer_id=account.stripe_customer_id,
            period_start=period,
            from_block=from_block,
            to_block=quantity,
        ),
    )
    account.last_reported_blocks = quantity
    await session.flush()
    return delta


async def report_accrued_usage(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    gateway: StripeGateway | None = None,
    now: datetime | None = None,
) -> int:
    """Flush blocks already accrued this period to Stripe, whatever the card state now is.

    Deliberately does NOT consult ``billing_state``: this is the path that bills a tenant
    who ran 250 conversations and then detached their card. ``build_budget`` reports zero
    blocks the moment the card is gone and ``report_usage`` refuses a non-payg account, so
    without this the two blocks they had already run were silently voided — a free-usage
    path anyone could take deliberately.

    Needs no instance because the accrual is a per-tenant, per-period total on the account.
    """
    gateway = gateway or get_stripe_gateway()
    now = now or datetime.now(UTC)
    account = await get_or_create_billing_account(session, admin_id=admin_id)
    if account.accrued_blocks <= account.last_reported_blocks:
        return 0
    # The residual belongs to the period it accrued in, which is not necessarily the
    # current one (the counter may have rolled since the last turn).
    period = account.accrual_period_start or period_start_for(account, now)
    if account.stripe_subscription_id is None:
        # Nothing was ever metered against this tenant — either the free 50 was never
        # exceeded with a card, or the metered price was unbound when it was. The
        # latter is the alarm case (round 7 WP-1 #4); the residual stays on the
        # watermark either way.
        await _alarm_meter_unbound(session, account=account, period=period)
        return 0
    return await _post_usage(
        session, account=account, quantity=account.accrued_blocks, gateway=gateway, period=period
    )
