"""Close-account purge sweep (Architecture §3.6.6, Legal §B5).

``cascade.close_account`` puts the tenant's Luciel into its 30-day grace window and
flips the account shell to ``closed``. What it deliberately does NOT do is destroy
anything — Legal §B5 promises the closure is recoverable through the same 30-day
window as a Luciel deletion, and only then is the credential material (email +
password hash) permanently deleted, which is also what releases the email address
for signup again. This sweep is where the Luciel cascade, the billing identity
(Stripe customer retired first) and the account row all go.

This module is the "and then permanently deleted" half. The closure instant is
``accounts.closed_at`` (migration 0068), stamped by ``cascade.close_account`` and
cleared by the D5 reopen. Rows closed before that column existed carry NULL and fall
back to the tenant's ``account_closure_initiated`` audit event, which is append-only
and therefore a trustworthy clock (§5.3). The audit row alone stopped being enough
with owner decision D5 (2026-09-14): a closed account can be reopened and closed
again, and the FIRST closure row would purge a re-closed account on the spot. Audit
tombstones survive the purge — the log is owned by ``luciel_audit_owner`` and is not
tenant-cascade material (§3.6.6).

Shape mirrors ``grace_expiry``/``retention``: a per-tenant one-shot that CI/tests
call directly; cross-tenant discovery + the cron cadence live in ``app/jobs``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.config import get_settings
from app.lifecycle.cascade import hard_delete_cascade
from app.models.audit import ActorType, AdminAuditLog
from app.models.instance import Account, AccountState

CLOSURE_EVENT = "account_closure_initiated"


def reopen_deadline(closed_at: datetime) -> datetime:
    """The instant the 30-day window ends: the last moment the owner can reopen or
    re-request the export (D5), and the first moment the purge may run (§B5). One
    function so the reduced surface and the purge can never disagree by a day."""
    return closed_at + timedelta(days=get_settings().grace_window_days)


async def closure_started_at(
    session: AsyncSession, admin_id: uuid.UUID, *, account: Account | None = None
) -> datetime | None:
    """Return when the CURRENT closure began for this tenant.

    ``accounts.closed_at`` when it is stamped (every closure since migration 0068).
    Otherwise — a row closed before the column existed — the FIRST
    ``account_closure_initiated`` audit row: such a tenant has closed exactly once
    (reopen did not exist), and a repeated close call must not restart the clock.
    Pass ``account`` when the row is already loaded to save the read."""
    if account is None:
        account = (
            await session.execute(select(Account).where(Account.admin_id == admin_id))
        ).scalar_one_or_none()
    if account is not None and account.closed_at is not None:
        stamped = account.closed_at
        return stamped if stamped.tzinfo else stamped.replace(tzinfo=UTC)
    row = (
        await session.execute(
            select(AdminAuditLog.created_at)
            .where(
                AdminAuditLog.admin_id == admin_id,
                AdminAuditLog.event_type == CLOSURE_EVENT,
            )
            .order_by(AdminAuditLog.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is not None and row.tzinfo is None:
        return row.replace(tzinfo=UTC)
    return row


async def purge_closed_account_if_due(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    now: datetime | None = None,
) -> bool:
    """Permanently delete ONE closed account's shell iff its grace window expired.

    Returns True when the account row was purged, False when the tenant is not
    ``closed``, has no closure audit event, or is still inside the window.
    Idempotent: once purged there is no account row left to match, and a second run
    over the same tenant is a no-op. Must be called inside a tenant-scoped session
    for ``admin_id`` so RLS scopes the audit read, the cascade, and the delete.
    """
    now = now or datetime.now(UTC)
    # Row-locked (D5): the owner's reopen takes the same lock first. Whichever commits
    # first wins cleanly — a reopen that lands a breath before this sweep is re-read
    # here as ``verified`` and nothing is purged; a purge that lands first leaves the
    # reopen no row to act on. Without the lock the sweep could read ``closed``,
    # lose the race, and destroy an account its owner had just got back.
    acct = (
        await session.execute(select(Account).where(Account.admin_id == admin_id).with_for_update())
    ).scalar_one_or_none()
    if acct is None or acct.state != AccountState.closed:
        return False

    started = await closure_started_at(session, admin_id, account=acct)
    if started is None:
        # Closed without a closure event is a data-integrity anomaly; do NOT destroy
        # credentials on a guess (same posture as an unstamped grace window).
        return False
    if now < reopen_deadline(started):
        return False

    # The whole tenant goes here: the Luciel (if the grace-expiry sweep has not already
    # taken it), and — account scope — the billing identity. Unconditional because
    # tenant data does not require a Luciel to exist: an account that attached a card
    # and connected Google without finishing onboarding still owns billing rows and
    # credentials, and this is the last caller that could ever remove them.
    await hard_delete_cascade(session, admin_id, include_account_scope=True)

    await audit.emit(
        session,
        admin_id=admin_id,
        actor_type=ActorType.platform,
        event_type="lifecycle_transition",
        payload={
            "from": "account_closing",
            "to": "account_purged",
            "trigger": "close_account_purge",
            "closure_initiated_at": started.isoformat(),
        },
    )
    await session.execute(delete(Account).where(Account.admin_id == admin_id))
    await session.flush()
    return True
