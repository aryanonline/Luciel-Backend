"""Single-login authentication + account lifecycle (Architecture §3.7.1 / §3.7.1a).

DOCTRINE ANCHOR (Architecture §8: app/auth/access.py). Any change to this
interface requires updating Architecture §3.7 in the same PR.

Session orchestration & isolation:
  - The ONLY pre-subject read is the email→account lookup (app/auth/lookup.py),
    backed by a narrow SECURITY DEFINER function.
  - Every account-row read/write runs inside ``tenant_session(account_id)`` so
    Layer-3 ``app.admin_id`` is pinned to the acting account (accounts are
    self-tenant: admin_id == id). RLS therefore scopes the account + its audit
    rows to itself.

Implements signup, hard verification gate, login, logout, verify, resend
(3/15min), forgot/reset (1h TTL, single-use, reset→global revocation via epoch).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import log as audit
from app.auth.lookup import lookup_account_by_email
from app.auth.passwords import hash_password, verify_password
from app.auth.tokens import mint_link_token, mint_session_token
from app.config import get_settings
from app.db.session import tenant_session, untenanted_session
from app.models.audit import ActorType
from app.models.instance import Account, AccountState, Instance


def _now() -> datetime:
    return datetime.now(UTC)


def next_route_for_state(state: AccountState, email_verified: bool, has_luciel: bool) -> str:
    if state == AccountState.unverified or not email_verified:
        return "verify_wall"
    return "dashboard" if has_luciel else "first_run"


async def _has_luciel(session: AsyncSession, admin_id: uuid.UUID) -> bool:
    return (
        await session.execute(select(Instance.id).where(Instance.admin_id == admin_id).limit(1))
    ).scalar_one_or_none() is not None


# --------------------------------------------------------------------------- #
# Signup                                                                       #
# --------------------------------------------------------------------------- #
async def signup(email: str, password: str) -> tuple[Account, str]:
    """Create or overwrite a pending unverified account; return (account, verify_token).

    Re-signup on a pending email overwrites it (non-enumerating). A verified email
    is left intact and no verify token is issued (router maps both to the same
    generic shape to avoid enumeration).
    """
    settings = get_settings()
    async with untenanted_session() as s:
        found = await lookup_account_by_email(s, email)

    if found is not None and found.state in ("verified", "closed", "pending_deletion"):
        # Verified owner — return existing account without overwrite or reissue.
        # A CLOSED account is the same case (round 7 WP-5 / D5): its email is not
        # released for signup until the day-30 purge deletes the row. Falling through
        # to the overwrite below let anyone who knew the address replace the owner's
        # password and flip the account to ``unverified`` — which locked the owner out
        # of the reopen / re-export surface and took the row out of the purge's reach.
        async with tenant_session(found.account_id) as s:
            acct = (
                await s.execute(select(Account).where(Account.id == found.account_id))
            ).scalar_one()
            # Detach a copy for the caller.
            return _detach(acct), ""

    if found is not None:
        # Overwrite the pending record under its own tenant scope.
        async with tenant_session(found.account_id) as s:
            acct = (
                await s.execute(select(Account).where(Account.id == found.account_id))
            ).scalar_one()
            acct.password_hash = hash_password(password)
            acct.email = email
            acct.email_lower = email.lower()
            acct.state = AccountState.unverified
            acct.email_verified = False
            acct.session_epoch += 1
            jti = uuid.uuid4().hex
            acct.pending_verify_jti = jti
            await s.flush()
            await audit.emit(
                s, admin_id=acct.id, event_type="account_created", actor_type=ActorType.platform
            )
            token = mint_link_token(acct.id, "verify", settings.verification_link_ttl_seconds, jti)
            return _detach(acct), token

    # Fresh account.
    admin_id = uuid.uuid4()
    jti = uuid.uuid4().hex
    async with tenant_session(admin_id) as s:
        acct = Account(
            id=admin_id,
            admin_id=admin_id,
            user_id=uuid.uuid4(),
            email=email,
            email_lower=email.lower(),
            password_hash=hash_password(password),
            state=AccountState.unverified,
            email_verified=False,
            pending_verify_jti=jti,
        )
        s.add(acct)
        await s.flush()
        await audit.emit(
            s, admin_id=admin_id, event_type="account_created", actor_type=ActorType.platform
        )
        token = mint_link_token(admin_id, "verify", settings.verification_link_ttl_seconds, jti)
        return _detach(acct), token


# --------------------------------------------------------------------------- #
# Verify / resend                                                              #
# --------------------------------------------------------------------------- #
async def verify_email(admin_id: uuid.UUID, jti: str) -> Account:
    async with tenant_session(admin_id) as s:
        acct = (await s.execute(select(Account).where(Account.id == admin_id))).scalar_one_or_none()
        if acct is None:
            raise ValueError("not_found")
        if acct.pending_verify_jti != jti:
            raise ValueError("invalid_or_used")
        acct.state = AccountState.verified
        acct.email_verified = True
        acct.pending_verify_jti = None
        await s.flush()
        await audit.emit(
            s, admin_id=admin_id, event_type="email_verified", actor_type=ActorType.platform
        )
        return _detach(acct)


async def reissue_verification(admin_id: uuid.UUID) -> tuple[str | None, int | None]:
    settings = get_settings()
    async with tenant_session(admin_id) as s:
        acct = (await s.execute(select(Account).where(Account.id == admin_id))).scalar_one_or_none()
        if acct is None or acct.state != AccountState.unverified:
            return None, None
        now = _now()
        window = settings.resend_limit_window_seconds
        if (
            acct.resend_window_started_at is None
            or (now - acct.resend_window_started_at).total_seconds() >= window
        ):
            acct.resend_window_started_at = now
            acct.resend_count = 0
        if acct.resend_count >= settings.resend_limit_count:
            elapsed = (now - acct.resend_window_started_at).total_seconds()
            return None, int(window - elapsed)
        acct.resend_count += 1
        jti = uuid.uuid4().hex
        acct.pending_verify_jti = jti
        await s.flush()
        await audit.emit(
            s, admin_id=admin_id, event_type="verification_resent", actor_type=ActorType.platform
        )
        return mint_link_token(
            admin_id, "verify", settings.verification_link_ttl_seconds, jti
        ), None


# --------------------------------------------------------------------------- #
# Login                                                                        #
# --------------------------------------------------------------------------- #
async def login(email: str, password: str) -> tuple[Account, str, str] | None:
    """Return (account, session_token, next_route) or None on bad credentials."""
    async with untenanted_session() as s:
        found = await lookup_account_by_email(s, email)
    if found is None or not verify_password(found.password_hash, password):
        if found is not None:
            async with tenant_session(found.account_id) as s:
                await audit.emit(
                    s,
                    admin_id=found.account_id,
                    event_type="login_failed",
                    actor_type=ActorType.platform,
                )
        return None

    async with tenant_session(found.account_id) as s:
        acct = (await s.execute(select(Account).where(Account.id == found.account_id))).scalar_one()
        has_luciel = await _has_luciel(s, acct.id)
        next_route = next_route_for_state(acct.state, acct.email_verified, has_luciel)
        await audit.emit(
            s, admin_id=acct.id, event_type="login_succeeded", actor_type=ActorType.platform
        )
        token = mint_session_token(acct.id, acct.user_id, acct.session_epoch)
        return _detach(acct), token, next_route


# --------------------------------------------------------------------------- #
# Password reset                                                               #
# --------------------------------------------------------------------------- #
async def request_reset(email: str) -> str | None:
    settings = get_settings()
    async with untenanted_session() as s:
        found = await lookup_account_by_email(s, email)
    if found is None:
        return None
    async with tenant_session(found.account_id) as s:
        acct = (await s.execute(select(Account).where(Account.id == found.account_id))).scalar_one()
        jti = uuid.uuid4().hex
        acct.pending_reset_jti = jti
        await s.flush()
        await audit.emit(
            s,
            admin_id=acct.id,
            event_type="password_reset_requested",
            actor_type=ActorType.platform,
        )
        return mint_link_token(acct.id, "reset", settings.reset_link_ttl_seconds, jti)


async def complete_reset(admin_id: uuid.UUID, jti: str, new_password: str) -> None:
    async with tenant_session(admin_id) as s:
        acct = (await s.execute(select(Account).where(Account.id == admin_id))).scalar_one_or_none()
        if acct is None or acct.pending_reset_jti != jti:
            raise ValueError("invalid_or_used")
        acct.password_hash = hash_password(new_password)
        acct.pending_reset_jti = None
        acct.session_epoch += 1  # revoke all sessions
        await s.flush()
        await audit.emit(
            s,
            admin_id=admin_id,
            event_type="password_reset_completed",
            actor_type=ActorType.platform,
            payload={"all_sessions_revoked": True},
        )


def _detach(acct: Account) -> Account:
    """Return a plain detached Account snapshot the caller can read after the txn."""
    snap = Account(
        id=acct.id,
        admin_id=acct.admin_id,
        user_id=acct.user_id,
        email=acct.email,
        email_lower=acct.email_lower,
        password_hash="",  # nosec B106 — deliberately blanked; never expose the real hash
        state=acct.state,
        email_verified=acct.email_verified,
        has_completed_first_run=acct.has_completed_first_run,
        session_epoch=acct.session_epoch,
    )
    snap.created_at = acct.created_at
    return snap
