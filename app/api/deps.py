"""Request dependencies — control-plane auth subject resolution.

The subject (admin_id + user_id) comes ONLY from the validated session JWT in the
httpOnly cookie (Architecture §3.7.2), never from a query param or body. The
resolved admin_id seeds Layer-3 ``app.admin_id`` for the request's tenant session.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import jwt
from fastapi import Request
from sqlalchemy import select

from app.api.errors import forbidden, unauthorized
from app.auth.tokens import decode_token
from app.config import get_settings
from app.db.session import tenant_session
from app.models.instance import Account, AccountState
from app.observability.context import bind_admin

#: Account states that must not reach the admin surface at all (P1-5). ``closed`` is the
#: one that bites: the row survives its 30-day recovery window (§3.6.6), so without this
#: a closed account keeps working until the cookie happens to expire. ``pending_deletion``
#: is not set anywhere yet — listing it now is what stops the same hole reopening the day
#: something does set it. Note the Luciel grace window lives on the INSTANCE state, not
#: here, so restore-my-Luciel access is untouched.
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


async def current_admin(request: Request) -> Subject:
    """Gated dependency: valid session AND verified, non-closed account.

    Closure is checked here and not only via the epoch bump in ``close_account``: the
    epoch revokes the tokens that exist at closure time, this refuses the ones minted
    afterwards (login still authenticates a closed account so the owner can be told
    what state they are in).
    """
    subject = await current_session_subject(request)
    acct = await _load_account(subject)
    if acct.state in CLOSED_ACCOUNT_STATES:
        raise forbidden("Account closed.")
    if acct.state == AccountState.unverified or not acct.email_verified:
        raise forbidden("Email verification required.")
    bind_admin(subject.admin_id)  # every later log line names the tenant (F035)
    return subject
