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

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.billing import dunning
from app.billing.stripe_gateway import StripeGateway, get_stripe_gateway
from app.config import get_settings
from app.models.billing import BillingAccount
from app.models.budget import ConversationBudget
from app.runtime.budget import current_billing_period_start


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
    """Dollar amount (in cents) for the period: blocks × $39 (the locked numbers).

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
    period = current_billing_period_start(now)
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
    has_live_card = account.billing_state == "payg_enabled" and not dunning.behaves_as_free_cap(
        account.dunning_state
    )

    at_cap = counted >= free and not has_live_card  # at_cap is data (§3.4.1b)

    # PAYG heads-up: only for a live-card account above the free allowance, true when
    # the account is >=80% through its CURRENT billed 100-block (position WITHIN the
    # block, not the whole period). e.g. over=80 -> 80/100 -> True; over=100 (a fresh
    # block just started) -> position 0 -> False. Honest early warning before a new
    # block is billed; never a cap.
    near_next_block = False
    if has_live_card and counted > free:
        position_in_block = (counted - free) % settings.payg_block_size
        near_next_block = position_in_block >= int(settings.payg_block_size * 0.8)

    # billedThisPeriod (wire COUNT) is only nonzero when billing actually applies —
    # i.e. there is a live card. A cardless account caps at free, so nothing billed.
    billed_count = billed_conversations(counted, free) if has_live_card else 0
    blocks = billed_blocks(counted, free, settings.payg_block_size) if has_live_card else 0
    cents = (
        billed_cents(counted, free, settings.payg_block_size, settings.payg_price_per_block_cents)
        if has_live_card
        else 0
    )

    # periodResetsAt: the Stripe period end (set on invoice.paid). Until a Stripe
    # period is established we surface the calendar-anchored fallback so the wire
    # field is always a valid timestamp (flagged: real anchor lands with Stripe).
    resets_at = account.period_resets_at or datetime(
        now.year + (1 if now.month == 12 else 0),
        1 if now.month == 12 else now.month + 1,
        1,
        tzinfo=UTC,
    )

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
    )


# --- idempotent Stripe metered-usage reporting (HARD CONSTRAINT #3) --------- #
def usage_idempotency_key(*, admin_id: uuid.UUID, period_start: datetime, quantity: int) -> str:
    """Idempotency key per (tenant, period, quantity) so a retry / webhook
    redelivery never double-bills (§3.4.1b)."""
    return f"usage:{admin_id}:{period_start.date().isoformat()}:{quantity}"


async def report_usage(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    now: datetime | None = None,
    gateway: StripeGateway | None = None,
) -> int:
    """Report this period's billed blocks to Stripe metered usage, idempotently.

    Returns the quantity reported (billed blocks). A no-op (returns 0) when there
    is no live subscription or nothing is billable. Reporting is keyed by
    (period, quantity) so a retry is deduped by the gateway (HARD CONSTRAINT #3)."""
    gateway = gateway or get_stripe_gateway()
    now = now or datetime.now(UTC)
    account = await get_or_create_billing_account(session, admin_id=admin_id)
    if account.stripe_subscription_id is None:
        return 0  # no metered subscription yet → nothing to report

    budget = await build_budget(session, admin_id=admin_id, instance_id=instance_id, now=now)
    quantity = budget.billed_blocks
    if quantity == 0:
        return 0

    period_start = account.period_start or datetime(now.year, now.month, 1, tzinfo=UTC)
    key = usage_idempotency_key(admin_id=admin_id, period_start=period_start, quantity=quantity)
    await gateway.report_metered_usage(
        subscription_id=account.stripe_subscription_id, quantity=quantity, idempotency_key=key
    )
    return quantity
