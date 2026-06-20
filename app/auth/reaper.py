"""Unverified-account reaper (Architecture §3.7.1a / Legal §A1 / §B5).

Architecture §3.7.1a: "An account that remains ``unverified`` for 7 days is
hard-purged — the email and password hash are deleted and the email becomes
available again."

Legal §A1: "…permanently deleted after 7 days…"

config.py ``unverified_reap_after_seconds`` (default 7 * 24 * 3600) defines the
window. A scheduled/periodic worker invokes ``reap_unverified_accounts()``; this
module owns the callable, not the scheduling infrastructure (no infra provisioned
here per Phase-2 constraints).

The reaper runs under a SUPERUSER/BYPASSRLS session (via the postgres superuser
psql path) so it can see ALL accounts regardless of tenant scope. In tests we
supply a plain tenant session since all test accounts share the same admin_id.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.config import get_settings
from app.models.audit import ActorType
from app.models.instance import Account, AccountState


async def reap_unverified_accounts(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> list[uuid.UUID]:
    """Hard-purge ``unverified`` accounts older than ``unverified_reap_after_seconds``.

    Deletes the account row (email + password_hash gone), emits
    ``unverified_account_reaped`` for each, and returns the list of reaped admin_ids.

    A scheduled job/worker calls this function periodically. No infra is provisioned
    here — the caller is responsible for the scheduling mechanism (celery beat, cron,
    asyncio background task, or a one-shot management command).

    Returns the list of admin_ids that were reaped so tests can assert on them.
    """
    now = now or datetime.now(UTC)
    settings = get_settings()
    window = timedelta(seconds=settings.unverified_reap_after_seconds)
    cutoff = now - window

    aged_accounts = (
        (
            await session.execute(
                select(Account).where(
                    Account.state == AccountState.unverified,
                    Account.created_at < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )

    reaped: list[uuid.UUID] = []
    for acct in aged_accounts:
        admin_id = acct.id
        # Emit the audit tombstone BEFORE deleting (evidence of the reap).
        await audit.emit(
            session,
            admin_id=admin_id,
            actor_type=ActorType.platform,
            event_type="unverified_account_reaped",
            payload={
                "reason": "7_day_unverified_window_expired",
                "reaped_at": now.isoformat(),
            },
        )
        await session.execute(delete(Account).where(Account.id == admin_id))
        reaped.append(admin_id)

    await session.flush()
    return reaped
