"""Reopen a closed account inside its 30-day window (owner decision D5, 2026-09-14).

``cascade.close_account`` is recoverable by design — it destroys nothing; the Luciel
goes through the same fused-quiesce delete every owner gets and the purge waits for
day 30 (§3.6.6, Legal §B5). What was missing was the way back: the account state was
terminal, so the "recoverable" closure could only be recovered by support. D5 gives
the owner the door: for 30 days they can sign in to a reduced surface and reopen.

Reopen restores the account **to the state before closure**:

* the account is open again (``verified``; ``closed_at`` cleared — the purge clock and
  the reduced surface both read it);
* the Luciel comes back **iff the closure is what deleted it** (the closure's own audit
  row says so — ``cascade.closure_record``), through the ordinary
  ``state_machine.restore`` (same embed key, connections at their pre-delete status,
  knowledge and CRM links re-qualified), and is paused again if it was paused. A
  Luciel the owner had deleted BEFORE closing stays deleted — that was their decision,
  and its own Restore (same grace window) is still there;
* billing resumes on the arrangement it had: closure settled the outstanding usage and
  ended the metered subscription with a final invoice, but left the card, the Stripe
  customer and ``billing_state`` alone, so nothing has to be re-collected. The
  subscription is NOT re-created here — it is bound lazily at the first billable block
  (``metering.report_usage``), exactly as for a tenant who has never been billed, so an
  owner who reopens and stays inside the free 50 is never handed a subscription. The
  outcome names which of the three postures applies, and the audit row records it.

Serialized against the day-30 purge by a row lock on the account (both take ``FOR
UPDATE`` first): a reopen that commits a breath before the sweep makes the sweep a
no-op instead of destroying an account its owner just got back.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.lifecycle import cascade, state_machine
from app.lifecycle.close_purge import closure_started_at, reopen_deadline
from app.lifecycle.state_machine import LifecycleError
from app.models.audit import ActorType
from app.models.billing import BillingAccount
from app.models.instance import Account, AccountState, Instance, LucielState

#: ``ReopenOutcome.subscription`` — what stands behind the next billable block.
SUBSCRIPTION_LIVE = "live"  # closure could not end it; it never stopped
SUBSCRIPTION_REBINDS = "rebinds_on_next_billable_block"  # payg, card on file, lazy bind
SUBSCRIPTION_NONE = "none"  # free_cap (no card) or no billing account at all


@dataclass(frozen=True)
class ReopenOutcome:
    luciel_restored: bool
    luciel_state: str | None
    billing_state: str
    subscription: str


def _billing_posture(billing: BillingAccount | None) -> tuple[str, str]:
    if billing is None:
        return "free_cap", SUBSCRIPTION_NONE
    if billing.stripe_subscription_id is not None:
        return billing.billing_state, SUBSCRIPTION_LIVE
    if billing.billing_state == "payg_enabled" and billing.stripe_customer_id:
        return billing.billing_state, SUBSCRIPTION_REBINDS
    return billing.billing_state, SUBSCRIPTION_NONE


async def reopen_account(
    session: AsyncSession,
    admin_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> ReopenOutcome:
    """Reopen ``admin_id``'s closed account. Raises ``LifecycleError``:

    * ``account_not_closed`` — there is nothing to reopen (open account, or a state
      with no way back);
    * ``reopen_window_passed`` — day 30 has passed (or the closure instant is not on
      record: nothing is reopened on a guess, the same posture the purge takes).

    Must run inside a tenant-scoped session for ``admin_id``.
    """
    now = now or datetime.now(UTC)
    acct = (
        await session.execute(select(Account).where(Account.id == admin_id).with_for_update())
    ).scalar_one_or_none()
    if acct is None or acct.state != AccountState.closed:
        raise LifecycleError("account_not_closed")
    closed_at = await closure_started_at(session, admin_id, account=acct)
    if closed_at is None or now >= reopen_deadline(closed_at):
        raise LifecycleError("reopen_window_passed")

    record = await cascade.closure_record(session, admin_id)
    inst = (
        await session.execute(select(Instance).where(Instance.admin_id == admin_id))
    ).scalar_one_or_none()
    luciel_restored = False
    if (
        record.get("luciel_deleted_by_closure") is True
        and inst is not None
        and inst.state == LucielState.luciel_grace_window
    ):
        inst = await state_machine.restore(session, admin_id, actor_user_id)
        if record.get("luciel_state_before") == LucielState.paused.value:
            # "The state before closure": a Luciel the owner had paused must not come
            # back answering customers because the account was reopened.
            inst = await state_machine.pause(session, admin_id, actor_user_id)
        luciel_restored = True

    acct.state = AccountState.verified
    acct.closed_at = None
    await session.flush()

    billing = (
        await session.execute(select(BillingAccount).where(BillingAccount.admin_id == admin_id))
    ).scalar_one_or_none()
    billing_state, subscription = _billing_posture(billing)
    outcome = ReopenOutcome(
        luciel_restored=luciel_restored,
        luciel_state=inst.state.value if inst is not None else None,
        billing_state=billing_state,
        subscription=subscription,
    )
    await audit.emit(
        session,
        admin_id=admin_id,
        instance_id=inst.id if inst is not None else None,
        event_type="account_reopened",
        actor_type=ActorType.admin_user,
        actor_user_id=actor_user_id,
        payload={
            "closed_at": closed_at.isoformat(),
            "luciel_restored": outcome.luciel_restored,
            "luciel_state": outcome.luciel_state,
            "billing_state": outcome.billing_state,
            "subscription": outcome.subscription,
        },
    )
    await audit.emit(
        session,
        admin_id=admin_id,
        event_type="lifecycle_transition",
        actor_type=ActorType.admin_user,
        actor_user_id=actor_user_id,
        payload={"from": "account_closing", "to": "account_open", "trigger": "account_reopen"},
    )
    return outcome
