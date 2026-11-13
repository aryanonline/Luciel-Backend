"""A closed account must be closed everywhere (P1-5).

``deps.py`` carried the codebase's ONLY account-state check, and it rejected exactly one
state: ``unverified``. Closure set ``state = closed`` and nothing else, so a session
minted before closure stayed valid against the whole admin surface — on every device the
owner was signed in on — until the cookie happened to expire. The account row survives
closure by 30 days (§3.6.6), so "until it expires" is the real window, not a technicality.

Two independent halves, because neither covers the other:

  * the epoch bump kills the tokens that already exist at closure time;
  * the ``deps`` state check refuses the ones minted afterwards — login still
    authenticates a closed account, and must, so the owner can be told what state they
    are in rather than getting "wrong password".

The OAuth callback needs its own copy of the check: the provider redirects the browser
there with no session cookie, so ``current_admin`` never runs on it.
"""

from __future__ import annotations

import uuid

import pytest
from app.db.session import tenant_session
from app.lifecycle import cascade
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import select

pytestmark = [pytest.mark.contract]


async def _signup_verify_login(client) -> tuple[uuid.UUID, str, str]:
    from app.auth import access

    email = f"closed-{uuid.uuid4().hex[:8]}@x.com"
    password = "pw-123456"  # noqa: S105 — test fixture credential
    acct, token = await access.signup(email, password)
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    return acct.id, email, password


async def _seed_luciel(admin_id: uuid.UUID) -> None:
    async with tenant_session(admin_id) as s:
        s.add(
            Instance(
                id=uuid.uuid4(),
                admin_id=admin_id,
                name="L",
                state=LucielState.active,
                channels=[],
                tools=[],
                escalation={},
                personality={},
            )
        )


async def test_closing_the_account_revokes_the_session_already_in_flight(client) -> None:
    admin_id, _, _ = await _signup_verify_login(client)
    await _seed_luciel(admin_id)
    assert (await client.get("/api/v1/dashboard/conversations")).status_code == 200

    async with tenant_session(admin_id) as s:
        await cascade.close_account(s, admin_id, admin_id)

    # Same cookie jar, no re-login: the token the client is still holding is dead.
    assert (await client.get("/api/v1/dashboard/conversations")).status_code in (401, 403)


async def test_a_fresh_login_after_closure_still_cannot_reach_the_admin_surface(client) -> None:
    """The epoch bump alone is not enough — it only invalidates tokens that predate it."""
    admin_id, email, password = await _signup_verify_login(client)
    await _seed_luciel(admin_id)
    async with tenant_session(admin_id) as s:
        await cascade.close_account(s, admin_id, admin_id)

    login = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert login.status_code == 200, "closure must not masquerade as a bad password"
    assert (await client.get("/api/v1/dashboard/conversations")).status_code == 403


async def test_the_session_epoch_is_bumped_exactly_once_by_closure(client) -> None:
    admin_id, _, _ = await _signup_verify_login(client)
    async with tenant_session(admin_id) as s:
        before = (
            await s.execute(select(Account.session_epoch).where(Account.id == admin_id))
        ).scalar_one()
        await cascade.close_account(s, admin_id, admin_id)
    async with tenant_session(admin_id) as s:
        acct = (await s.execute(select(Account).where(Account.id == admin_id))).scalar_one()
    assert acct.session_epoch == before + 1
    assert acct.state == AccountState.closed


async def test_an_oauth_callback_cannot_land_credentials_on_a_closed_account() -> None:
    """The provider's redirect carries no session, so ``current_admin`` never runs on it.
    Without its own check, a consent screen opened before closure completes after it —
    writing a fresh secret for a tenant whose secrets the cascade already swept."""
    from app.api.v1.admin.connections import OAuthStateRejected, complete_oauth_connect

    admin_id = uuid.uuid4()
    async with tenant_session(admin_id) as s:
        s.add(
            Account(
                id=admin_id,
                admin_id=admin_id,
                user_id=admin_id,
                email=f"{admin_id}@x.com",
                email_lower=f"{admin_id}@x.com",
                password_hash="x",
                state=AccountState.closed,
                email_verified=True,
            )
        )

    with pytest.raises(OAuthStateRejected):
        await complete_oauth_connect(
            admin_id=admin_id,
            actor_user_id=None,
            connection_id=uuid.uuid4(),
            code="whatever",
            state="whatever",
        )
