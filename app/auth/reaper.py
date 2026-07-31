"""Unverified-account reaper (Architecture §3.7.1a / Legal §A1 / §B5).

Architecture §3.7.1a: "An account that remains ``unverified`` for 7 days is
hard-purged — the email and password hash are deleted and the email becomes
available again."

Legal §A1: "…permanently deleted after 7 days…"

config.py ``unverified_reap_after_seconds`` (default 7 * 24 * 3600) defines the
window.

Both entrypoints run under a NORMAL tenant-scoped session — the reaper is not a
BYPASSRLS job. ``accounts`` is self-tenanted (``admin_id`` mirrors the primary key),
so a tenant session already scopes the SELECT, the audit emit, and the DELETE to the
one account being reaped; nothing here needs to see across tenants. Cross-tenant
DISCOVERY is the sweep layer's job (``app.jobs.sweeps``), and it returns admin_ids
only.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.config import get_settings
from app.lifecycle.cascade import hard_delete_cascade
from app.models.audit import ActorType
from app.models.instance import Account, AccountState


def _cutoff(now: datetime) -> datetime:
    """Accounts created before this instant have outlived the verification window."""
    return now - timedelta(seconds=get_settings().unverified_reap_after_seconds)


async def _purge(session: AsyncSession, admin_id: uuid.UUID, now: datetime) -> None:
    """Emit the tombstone, THEN destroy the tenant. Reversing the order loses evidence.

    "Permanently deleted" is the whole tenant, not the ``accounts`` row. An abandoned
    signup is not necessarily an empty one — verification gates login, not onboarding,
    so a candidate can already own an instance, knowledge chunks with embeddings, and
    live OAuth credentials in Secrets Manager. Deleting only the shell left all of it
    addressed by an admin_id nothing would ever name again: unreachable, undeletable,
    still stored, and still billed for. The cascade is the same one closure runs, so
    the two paths cannot drift.
    """
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
    await hard_delete_cascade(session, admin_id)
    await session.execute(delete(Account).where(Account.id == admin_id))


async def reap_account_if_due(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    now: datetime | None = None,
) -> bool:
    """Hard-purge ONE account iff it is still ``unverified`` past the window.

    The per-tenant primitive the ``unverified_reap`` sweep drives, shaped like every
    other sweep unit (``purge_closed_account_if_due``, ``dunning.advance_if_due``): it
    re-derives its own precondition rather than trusting the candidate list, so a
    verification that landed between discovery and this call is respected and a repeat
    run over the same tenant is a no-op. Must be called inside a tenant session for
    ``admin_id``.

    Returns True when the account row was destroyed.
    """
    now = now or datetime.now(UTC)
    acct = (
        await session.execute(
            select(Account).where(
                Account.admin_id == admin_id,
                Account.state == AccountState.unverified,
                Account.created_at < _cutoff(now),
            )
        )
    ).scalar_one_or_none()
    if acct is None:
        return False
    await _purge(session, acct.id, now)
    await session.flush()
    return True


async def reap_unverified_accounts(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> list[uuid.UUID]:
    """Reap every aged ``unverified`` account VISIBLE to ``session``.

    The direct/one-shot form: it purges whatever the session can see, which under a
    tenant session is that tenant alone. The scheduled path does not go through here —
    it discovers candidates cross-tenant and calls :func:`reap_account_if_due` once per
    tenant, so one tenant's failure cannot roll back another's.

    Returns the reaped admin_ids.
    """
    now = now or datetime.now(UTC)
    aged = (
        (
            await session.execute(
                select(Account.id).where(
                    Account.state == AccountState.unverified,
                    Account.created_at < _cutoff(now),
                )
            )
        )
        .scalars()
        .all()
    )
    for admin_id in aged:
        await _purge(session, admin_id, now)
    await session.flush()
    return list(aged)
