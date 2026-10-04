"""Request dependencies — control-plane auth subject resolution.

The subject (admin_id + user_id) comes ONLY from the validated session JWT in the
httpOnly cookie (Architecture §3.7.2), never from a query param or body. The
resolved admin_id seeds Layer-3 ``app.admin_id`` for the request's tenant session.

Three gates, narrowest last:

* ``current_session_subject`` — a valid, unrevoked session. ``/auth/me`` and logout.
* ``current_account_owner`` — verified, and either open or CLOSED INSIDE ITS 30-DAY
  REOPEN WINDOW (owner decision D5, 2026-09-14). The reduced closed-account surface
  and nothing else: read the account status, reopen, re-request the export.
* ``current_admin`` — verified and open. Every other admin route. A closed account
  gets the typed 403 ``account_closed`` (``detail.reopenUntil``) so the frontend
  renders the reduced page instead of bouncing to login.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import jwt
from fastapi import Request
from sqlalchemy import select

from app.api.errors import ApiError, account_closed, forbidden, unauthorized
from app.auth.tokens import decode_token
from app.config import get_settings
from app.db.session import tenant_session
from app.models.instance import Account, AccountState
from app.observability.context import bind_admin

#: Account states that must not reach the admin surface (P1-5). ``closed`` is the
#: one that bites: the row survives its 30-day recovery window (§3.6.6), so without this
#: a closed account keeps working until the cookie happens to expire. ``pending_deletion``
#: is not set anywhere yet — listing it now is what stops the same hole reopening the day
#: something does set it. Note the Luciel grace window lives on the INSTANCE state, not
#: here, so restore-my-Luciel access is untouched.
#:
#: D5 carve-out: a ``closed`` account inside its reopen window reaches the THREE routes
#: that depend on ``current_account_owner`` — and only those. ``pending_deletion`` has
#: no way back and reaches nothing.
CLOSED_ACCOUNT_STATES = frozenset({AccountState.closed, AccountState.pending_deletion})


@dataclass
class Subject:
    admin_id: uuid.UUID
    user_id: uuid.UUID
    session_epoch: int


def _read_session_cookie(request: Request) -> str:
    token = request.cookies.get(get_settings().session_cookie_name)
    if not token:
        raise unauthorized("No session.")
    return token


async def _decode_session(request: Request) -> Subject:
    token = _read_session_cookie(request)
    try:
        claims = decode_token(token)
    except jwt.PyJWTError as exc:
        raise unauthorized("Invalid or expired session.") from exc
    if claims.get("typ") != "session":
        raise unauthorized("Wrong token type.")
    return Subject(
        admin_id=uuid.UUID(claims["sub"]),
        user_id=uuid.UUID(claims["uid"]),
        session_epoch=int(claims.get("ep", 0)),
    )


async def _load_account(subject: Subject) -> Account:
    async with tenant_session(subject.admin_id) as s:
        acct = (
            await s.execute(select(Account).where(Account.id == subject.admin_id))
        ).scalar_one_or_none()
    if acct is None:
        raise unauthorized("Account not found.")
    return acct


async def current_session_subject(request: Request) -> Subject:
    """Valid session required; epoch must match (global-revocation check).

    Used by /auth/me, which must work for unverified accounts too (so it can
    return nextRoute='verify_wall').
    """
    subject = await _decode_session(request)
    acct = await _load_account(subject)
    if (acct.session_epoch or 0) != subject.session_epoch:
        raise unauthorized("Session revoked.")
    return subject


@dataclass(frozen=True)
class ClosureWindow:
    """When a closed account closed and until when it can be reopened (D5). Both are
    ``None`` when the closure instant is not on record (a closed row with neither
    ``closed_at`` nor a closure audit event — a data anomaly; nothing is reopenable
    on a guess, the same posture the purge takes)."""

    closed_at: datetime | None
    reopen_until: datetime | None

    def open_at(self, now: datetime) -> bool:
        return self.reopen_until is not None and now < self.reopen_until


async def closure_window(acct: Account) -> ClosureWindow:
    """The reopen window of a closed account — the same clock and the same deadline
    the day-30 purge uses (``close_purge``), so the surface and the purge agree."""
    # Lazy: lifecycle pulls in billing/connections, which this module must not import
    # at load (every router imports deps).
    from app.lifecycle.close_purge import closure_started_at, reopen_deadline  # noqa: PLC0415

    async with tenant_session(acct.id) as s:
        started = await closure_started_at(s, acct.id, account=acct)
    if started is None:
        return ClosureWindow(closed_at=None, reopen_until=None)
    return ClosureWindow(closed_at=started, reopen_until=reopen_deadline(started))


def _closed_refusal(window: ClosureWindow) -> ApiError:
    return account_closed(
        closed_at=window.closed_at.isoformat() if window.closed_at else None,
        reopen_until=window.reopen_until.isoformat() if window.reopen_until else None,
    )


async def current_admin(request: Request) -> Subject:
    """Gated dependency: valid session AND verified, non-closed account.

    Closure is checked here and not only via the epoch bump in ``close_account``: the
    epoch revokes the tokens that exist at closure time, this refuses the ones minted
    afterwards (login still authenticates a closed account so the owner can be told
    what state they are in). The refusal is the typed 403 ``account_closed`` carrying
    ``reopenUntil`` (D5) — the frontend renders the reduced page from it.
    """
    subject = await current_session_subject(request)
    acct = await _load_account(subject)
    if acct.state in CLOSED_ACCOUNT_STATES:
        raise _closed_refusal(await closure_window(acct))
    if acct.state == AccountState.unverified or not acct.email_verified:
        raise forbidden("Email verification required.")
    bind_admin(subject.admin_id)  # every later log line names the tenant (F035)
    return subject


async def current_account_owner(request: Request) -> Subject:
    """The D5 reduced surface: a verified account that is open, OR closed and still
    inside its 30-day reopen window.

    Owner decision D5 (2026-09-14): after closure the owner can sign in for 30 days to
    a surface that offers exactly two actions — reopen the account, and re-request the
    export. ONLY the routes that serve that surface depend on this (``GET
    /admin/account``, ``POST /admin/account/reopen``, ``POST /admin/account/export``);
    every other admin route depends on ``current_admin`` and keeps refusing. Once the
    window has passed — or for ``pending_deletion``, which has no way back — this
    refuses with the same typed 403, ``reopenUntil`` now in the past.

    The session is an ordinary one: closure bumped the epoch (killing every token that
    existed then), and the login that follows mints a fresh token at the new epoch.
    """
    subject = await current_session_subject(request)
    acct = await _load_account(subject)
    if acct.state == AccountState.unverified or not acct.email_verified:
        raise forbidden("Email verification required.")
    if acct.state in CLOSED_ACCOUNT_STATES:
        window = await closure_window(acct)
        if acct.state != AccountState.closed or not window.open_at(datetime.now(UTC)):
            raise _closed_refusal(window)
    bind_admin(subject.admin_id)
    return subject
