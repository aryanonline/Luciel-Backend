"""Conversation budget gate (Architecture §3.4.1b).

The agentic loop checks the monthly budget BEFORE entering PLAN. The gate answers
two questions:
  - is the tenant at/over the free allowance with no payment method? (at_cap)
  - how many conversations have been counted this billing period?

Counter precision (§3.4.1b): the counter increments exactly ONCE per session,
firing on the first of (a) the session's first model call, or (b) the session
becoming human-controlled before any model call. The increment is idempotent on
the session identifier — the counted-session set guards against double counting
across the model-call path and the takeover path.

Storage model (§3.4.1b): Postgres is the durable source of truth; Redis is the
hot read cache. The write is synchronous write-through — Postgres is written
before the conversation is served, so the durable count is never behind the
served count, and a Redis failure can never lose an increment.

BUILD STATUS (honest disclosure, mirrors §3.4.1b): full metering — Stripe overage
counting, the 80%/100-block notifications, and the period reset on the Stripe
invoice.paid webhook — is Arc 18. Arc 14 implements only the gate and the
fire-once write-through counter against the Postgres source of truth. The Redis
hot cache is best-effort here and the §4.5 Redis-down fallback is therefore NOT
yet claimed operative; Postgres is read directly. PAYG (continue-and-bill) above
the free allowance is Arc 18 — until a payment method + metering land, behavior
caps at the free allowance, which is the documented no-payment-method behavior.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.billing import BillingAccount
from app.models.budget import ConversationBudget, CountedSession


def current_billing_period_start(now: datetime | None = None) -> date:
    """Calendar-month anchor — the fallback for a tenant Stripe has never invoiced.

    Only correct before a Stripe period exists. Once one does, ``period_start_for``
    uses it instead; see that function for why the two must not diverge."""
    now = now or datetime.now(UTC)
    return date(now.year, now.month, 1)


def period_start_for(account: BillingAccount | None, now: datetime | None = None) -> date:
    """The period the conversation counter is keyed on (§3.4.1b).

    Reset is driven by Stripe ``invoice.paid``, so the anchor is the account's
    Stripe-anchored ``period_start``; the calendar month is used only when no billing
    account exists yet and there is therefore no Stripe period to anchor on.

    Keying on the calendar instead zeroed the counter on the 1st for a tenant whose
    Stripe period runs (say) the 15th to the 15th — a second free 50 every month — while
    the Stripe usage idempotency key still named the Stripe period, so the second block
    crossed in the same Stripe period reused a spent key and was never billed at all.
    """
    if account is not None and account.period_start is not None:
        return account.period_start.astimezone(UTC).date()
    return current_billing_period_start(now)


async def resolve_billing_period_start(
    session: AsyncSession, *, admin_id: uuid.UUID, now: datetime | None = None
) -> date:
    """``period_start_for`` against the tenant's billing account, read not created.

    The gate runs on every turn and must not conjure a billing account as a side
    effect — a tenant with no account simply gets the calendar fallback.
    """
    account = (
        await session.execute(select(BillingAccount).where(BillingAccount.admin_id == admin_id))
    ).scalar_one_or_none()
    return period_start_for(account, now)


@dataclass
class BudgetState:
    counted: int
    free_allowance: int
    # at_cap surfaces as DATA (budget.atCap), not a thrown error (BUILD_SPEC §4 /
    # frozen widgetRenderState 'at_cap'). True when the free allowance is reached
    # and no PAYG payment method is on file.
    at_cap: bool

    @property
    def remaining(self) -> int:
        return max(0, self.free_allowance - self.counted)


async def _get_or_create_row(
    session: AsyncSession, *, admin_id: uuid.UUID, instance_id: uuid.UUID, period: date
) -> ConversationBudget:
    """The period's counter row, created if this is the tenant's first conversation.

    ON CONFLICT DO NOTHING rather than SELECT-then-INSERT: two concurrent
    first-conversations of a period both used to INSERT, and the loser violated
    ``uq_budget_period`` — a 500 on a real customer's message.
    """
    await session.execute(
        pg_insert(ConversationBudget)
        .values(
            id=uuid.uuid4(),
            admin_id=admin_id,
            instance_id=instance_id,
            billing_period_start=period,
            counted_conversations=0,
            counted_session_ids=[],
        )
        .on_conflict_do_nothing(index_elements=["admin_id", "instance_id", "billing_period_start"])
    )
    return (
        await session.execute(
            select(ConversationBudget).where(
                ConversationBudget.admin_id == admin_id,
                ConversationBudget.instance_id == instance_id,
                ConversationBudget.billing_period_start == period,
            )
        )
    ).scalar_one()


async def check_budget(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    has_payment_method: bool = False,
) -> BudgetState:
    """Read the gate state without mutating the counter (run BEFORE PLAN, §3.4.1b).

    at_cap is true once the free allowance is consumed AND no payment method is on
    file. With a payment method, PAYG would continue serving (Arc 18) — here we
    report not-at-cap so the loop semantics are correct once metering lands, but the
    actual overage billing is Arc 18 (flagged above)."""
    settings = get_settings()
    period = await resolve_billing_period_start(session, admin_id=admin_id)
    row = await _get_or_create_row(
        session, admin_id=admin_id, instance_id=instance_id, period=period
    )
    free = settings.free_allowance  # literally 50 (§3.4.1b / BUILD_SPEC §6)
    at_cap = row.counted_conversations >= free and not has_payment_method
    return BudgetState(counted=row.counted_conversations, free_allowance=free, at_cap=at_cap)


async def count_session_once(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    session_id: uuid.UUID,
) -> bool:
    """Fire-once, idempotent increment for a session (§3.4.1b Counter precision).

    Returns True if THIS call performed the increment, False if the session was
    already counted. Called at the fire-once trigger: the first model call, or a
    human-controlled handoff before any model call. Write-through to the Postgres
    source of truth happens here (synchronously, before serving).

    Postgres decides who counted, not Python. The insert into ``counted_sessions``
    either wins the unique constraint or does nothing, and the increment is a single
    ``SET n = n + 1``. Reading the list, testing membership and writing back raced on
    both halves: concurrent turns on one session double-counted and clobbered each
    other's session ids, and concurrent turns on different sessions lost an increment
    outright.
    """
    period = await resolve_billing_period_start(session, admin_id=admin_id)
    await _get_or_create_row(session, admin_id=admin_id, instance_id=instance_id, period=period)

    claimed = await session.execute(
        pg_insert(CountedSession)
        .values(
            id=uuid.uuid4(),
            admin_id=admin_id,
            instance_id=instance_id,
            session_id=session_id,
            billing_period_start=period,
        )
        .on_conflict_do_nothing(
            index_elements=["admin_id", "instance_id", "session_id", "billing_period_start"]
        )
    )
    if claimed.rowcount == 0:  # type: ignore[attr-defined]  # another turn already counted this session
        return False

    await session.execute(
        update(ConversationBudget)
        .where(
            ConversationBudget.admin_id == admin_id,
            ConversationBudget.instance_id == instance_id,
            ConversationBudget.billing_period_start == period,
        )
        .values(counted_conversations=ConversationBudget.counted_conversations + 1)
    )
    await session.flush()
    return True
