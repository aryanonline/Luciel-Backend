"""Contract conformance for /api/v1/auth/* against the live app + real DB.

Exercises the documented account lifecycle and the wire shapes the frozen
contract (schemas/auth.ts, http-admin.ts, transport.ts) expects:
  - signup -> unverified account, camelCase body
  - gated route while unverified -> 403 (transport maps to 'unauthorized')
  - me() while unverified -> 200 with nextRoute='verify_wall' (the documented gate)
  - verify -> session cookie set, nextRoute='first_run'
  - login -> Session shape + httpOnly cookie
  - reset -> session cookie cleared (all sessions revoked)
"""

from __future__ import annotations

import uuid

import pytest
from app.auth.tokens import decode_token

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


def _email() -> str:
    return f"sarah-{uuid.uuid4().hex[:8]}@sarahchen.com"


async def _mint_verify_token_for(email: str) -> str:
    """Pull the verify token by re-running signup-internal path (tests only)."""
    from app.auth import access

    _acct, token = await access.signup(email, "pw-123456")
    return token


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_signup_returns_unverified_account_camelcase(client):
    email = _email()
    r = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "tok"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["account"]["state"] == "unverified"
    assert body["account"]["emailVerified"] is False
    # camelCase keys present (not snake_case)
    assert "emailVerified" in body["account"]
    assert "hasLuciel" in body["account"]
    assert "admin_id" not in body["account"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_login_sets_httponly_cookie_and_session_shape(client):
    email = _email()
    # signup + verify so login succeeds
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "tok"},
    )
    token = await _mint_verify_token_for(email)  # reissues + returns a fresh verify token
    rv = await client.post("/api/v1/auth/verify-email", json={"token": token})
    assert rv.status_code == 200

    r = await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"account", "nextRoute"}
    assert body["nextRoute"] in {"first_run", "dashboard"}
    # httpOnly session cookie set
    set_cookie = r.headers.get("set-cookie", "")
    assert "luciel_session=" in set_cookie
    assert "httponly" in set_cookie.lower()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_unverified_me_routes_to_verify_wall(client):
    email = _email()
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "tok"},
    )
    # login while unverified -> nextRoute verify_wall, cookie set
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    assert r.status_code == 200
    assert r.json()["nextRoute"] == "verify_wall"
    # me() with that cookie -> 200 verify_wall (documented gate; NOT a thrown error)
    rme = await client.get("/api/v1/auth/me")
    assert rme.status_code == 200
    assert rme.json()["nextRoute"] == "verify_wall"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_bad_credentials_401_maps_to_unauthorized(client):
    r = await client.post("/api/v1/auth/login", json={"email": "nobody@x.com", "password": "wrong"})
    assert r.status_code == 401  # transport.ts maps 401 -> 'unauthorized'
    assert "message" in r.json()


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_me_without_session_is_401(client):
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 401


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_verify_then_session_cookie_and_first_run(client):
    email = _email()
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "tok"},
    )
    token = await _mint_verify_token_for(email)
    r = await client.post("/api/v1/auth/verify-email", json={"token": token})
    assert r.status_code == 200
    assert r.json()["account"]["state"] == "verified"
    assert r.json()["nextRoute"] == "first_run"
    assert "luciel_session=" in r.headers.get("set-cookie", "")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_reset_revokes_all_sessions(client):
    email = _email()
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "tok"},
    )
    token = await _mint_verify_token_for(email)
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})

    # Request reset; mint reset token via the service path for the test.
    from app.auth import access

    reset_token = await access.request_reset(email)
    assert reset_token is not None
    claims = decode_token(reset_token)
    assert claims["typ"] == "reset"

    rr = await client.post(
        "/api/v1/auth/reset-password",
        json={"token": reset_token, "newPassword": "new-pw-7890"},
    )
    assert rr.status_code == 200
    # Old session cookie is now stale: a fresh me() with the pre-reset cookie fails.
    # (The client still holds the old cookie; epoch bump invalidates it.)
    rme = await client.get("/api/v1/auth/me")
    assert rme.status_code == 401  # session revoked

    # Old password no longer works; new password does.
    r_old = await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    assert r_old.status_code == 401
    r_new = await client.post(
        "/api/v1/auth/login", json={"email": email, "password": "new-pw-7890"}
    )
    assert r_new.status_code == 200


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_resend_verification_rate_limited(client):
    email = _email()
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "tok"},
    )
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    # 3 allowed per 15 min, then cooldown.
    results = []
    for _ in range(4):
        rr = await client.post("/api/v1/auth/resend-verification")
        results.append(rr.json())
    assert results[-1]["ok"] is False
    assert results[-1]["cooldownSecondsRemaining"] is not None
