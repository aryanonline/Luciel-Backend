"""The closed-account reduced surface over HTTP (round 7 WP-5 / owner decision D5).

Owner decision D5 (2026-09-14): after account closure the owner can sign in for 30
days to a REDUCED surface that offers exactly two actions — reopen the account and
re-request the export; nothing else is reachable.

Live condition before this: ``cascade.close_account`` set ``AccountState.closed`` with
no way back, and ``deps.current_admin`` refused every route with an untyped 403 the
frontend could not tell from "not signed in" — so the owner who closed by mistake
could log in (login authenticates a closed account) and then reach nothing at all,
not even the export the closure email promised could be re-requested.

``tests/unit/test_closed_account_surface.py`` pins the surface structurally (every
registered route, by dependency). This file drives the real thing: a closure through
the API, a fresh login, and the wire shapes the frontend renders from.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.db.session import tenant_session
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import select, update

pytestmark = [pytest.mark.contract]

PASSWORD = "pw-123456"  # noqa: S105 — test fixture credential


async def _signup_verify_login(client) -> tuple[uuid.UUID, str]:
    from app.auth import access

    email = f"d5-{uuid.uuid4().hex[:8]}@x.com"
    acct, token = await access.signup(email, PASSWORD)
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    return acct.id, email


async def _create_luciel(client) -> None:
    r = await client.post(
        "/api/v1/admin/luciel",
        json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"},
    )
    assert r.status_code in (200, 201), r.text


async def _close_and_sign_back_in(client, email: str) -> None:
    r = await client.post("/api/v1/admin/account/close", json={"confirmDeleteLuciel": True})
    assert r.status_code == 204, r.text
    login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200, "closure must not masquerade as a bad password"
    assert login.json()["account"]["state"] == "closed"


async def _backdate_closure(admin_id: uuid.UUID, *, days: int) -> None:
    async with tenant_session(admin_id) as s:
        await s.execute(
            update(Account)
            .where(Account.id == admin_id)
            .values(closed_at=datetime.now(UTC) - timedelta(days=days))
        )


def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_closure_kills_the_old_session_and_a_fresh_login_reads_the_account_status(client):
    """Session-epoch handling: closure bumps the epoch (every device is signed out),
    and the login that follows mints an ordinary session at the new epoch — which
    reaches the status read and nothing it could not reach before."""
    admin_id, email = await _signup_verify_login(client)
    await _create_luciel(client)
    before = datetime.now(UTC)
    r = await client.post("/api/v1/admin/account/close", json={"confirmDeleteLuciel": True})
    assert r.status_code == 204
    # The closure revoked every session, so the response also drops the cookie this
    # browser holds (as logout and a password reset do): a later page load meets the
    # plain login, never a 'session expired' claim (live walk run 7, 2026-10-25).
    cleared = r.headers.get("set-cookie", "")
    assert "luciel_session=" in cleared
    assert "Max-Age=0" in cleared or "expires=" in cleared.lower()
    assert "luciel_session" not in client.cookies

    # A cookie that predates the closure (another device) is revoked, not "closed".
    assert (await client.get("/api/v1/admin/account")).status_code == 401

    login = await client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200
    assert login.json()["account"]["state"] == "closed"

    status = await client.get("/api/v1/admin/account")
    assert status.status_code == 200, status.text
    body = status.json()
    assert body["state"] == "closed"
    assert body["canReopen"] is True
    assert body["lucielRestoredOnReopen"] is True
    closed_at, reopen_until = _iso(body["closedAt"]), _iso(body["reopenUntil"])
    assert before - timedelta(seconds=5) <= closed_at <= datetime.now(UTC) + timedelta(seconds=5)
    assert reopen_until - closed_at == timedelta(days=30)

    async with tenant_session(admin_id) as s:
        acct = (await s.execute(select(Account).where(Account.id == admin_id))).scalar_one()
        inst = (await s.execute(select(Instance))).scalar_one()
    assert acct.state == AccountState.closed
    assert acct.closed_at is not None
    assert inst.state == LucielState.luciel_grace_window


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_every_other_route_refuses_a_closed_account_with_the_typed_403(client):
    """The frontend renders the reduced page from this body: ``code`` says why,
    ``detail.reopenUntil`` says until when. A sample across every owner router — the
    structural test covers the rest by dependency."""
    _, email = await _signup_verify_login(client)
    await _create_luciel(client)
    await _close_and_sign_back_in(client, email)
    status = (await client.get("/api/v1/admin/account")).json()

    refused = [
        await client.get("/api/v1/admin/luciel"),
        await client.post("/api/v1/admin/luciel/restore"),
        await client.get("/api/v1/admin/knowledge/sources"),
        await client.get("/api/v1/admin/connections"),
        await client.get("/api/v1/admin/usage/audit-log"),
        await client.get("/api/v1/dashboard/conversations"),
        await client.get("/api/v1/billing"),
        # Closing again is not one of the two actions either.
        await client.post("/api/v1/admin/account/close", json={"confirmDeleteLuciel": True}),
    ]
    for r in refused:
        assert r.status_code == 403, (r.request.url.path, r.status_code, r.text)
        body = r.json()
        assert body["code"] == "account_closed", r.request.url.path
        assert body["message"] == "Account closed."
        assert _iso(body["detail"]["reopenUntil"]) == _iso(status["reopenUntil"])
        assert _iso(body["detail"]["closedAt"]) == _iso(status["closedAt"])

    # A bare session still reaches identity (so the shell can say "closed") and sign-out.
    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["account"]["state"] == "closed"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_open_account_reads_its_status_with_no_closure_instants(client):
    await _signup_verify_login(client)
    body = (await client.get("/api/v1/admin/account")).json()
    assert body == {
        "state": "verified",
        "closedAt": None,
        "reopenUntil": None,
        "canReopen": False,
        "lucielRestoredOnReopen": False,
    }


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_past_the_window_even_the_reduced_surface_is_gone(client):
    """Day 31, before the purge sweep has run: the row still exists and login still
    authenticates, but nothing is offered any more — and the body says the window
    is over (``reopenUntil`` in the past) rather than leaving the page to guess."""
    admin_id, email = await _signup_verify_login(client)
    await _close_and_sign_back_in(client, email)
    assert (await client.get("/api/v1/admin/account")).status_code == 200

    await _backdate_closure(admin_id, days=31)
    gone = await client.get("/api/v1/admin/account")
    assert gone.status_code == 403
    body = gone.json()
    assert body["code"] == "account_closed"
    assert _iso(body["detail"]["reopenUntil"]) < datetime.now(UTC)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_closure_from_before_the_column_reads_its_clock_from_the_audit_chain(client):
    """Rows closed before migration 0068 carry ``closed_at`` NULL. Their window is not
    "unknown": the closure audit event is the clock the purge has always used."""
    admin_id, email = await _signup_verify_login(client)
    await _close_and_sign_back_in(client, email)
    stamped = (await client.get("/api/v1/admin/account")).json()
    async with tenant_session(admin_id) as s:
        await s.execute(update(Account).where(Account.id == admin_id).values(closed_at=None))

    legacy = await client.get("/api/v1/admin/account")
    assert legacy.status_code == 200
    body = legacy.json()
    assert body["canReopen"] is True
    assert abs(_iso(body["closedAt"]) - _iso(stamped["closedAt"])) < timedelta(seconds=10)
    assert _iso(body["reopenUntil"]) - _iso(body["closedAt"]) == timedelta(days=30)


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_unverified_session_does_not_reach_the_account_surface(client):
    """The reduced gate is not a way around the verification wall: signup issues a
    session to a pending account (for the resend), and that session reaches nothing."""
    email = f"d5-{uuid.uuid4().hex[:8]}@x.com"
    r = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": PASSWORD, "captchaToken": "t"},
    )
    assert r.status_code == 200
    refused = await client.get("/api/v1/admin/account")
    assert refused.status_code == 403
    assert "code" not in refused.json()
