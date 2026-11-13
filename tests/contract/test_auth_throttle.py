"""Sign-in and password-reset throttles (2026-09-05 audit F056/F068).

A wrong password could be tried without limit, and a reset link requested without
limit. Both are now bounded per email and per client address through the same token
buckets the channels use. A refusal is a 429 with a wait — and because a reset
request is throttled whether or not the address exists, the throttle enumerates
nothing (§3.7.1a).
"""

from __future__ import annotations

import uuid

import pytest
from app.channels import rate_limit

pytestmark = [pytest.mark.contract]


def _email() -> str:
    return f"throttle-{uuid.uuid4().hex[:8]}@x.com"


async def _register(client, email: str) -> None:
    from app.auth import access

    await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "pw-123456", "captchaToken": "t"}
    )
    _acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_repeated_wrong_passwords_are_refused_with_a_wait(client):
    email = _email()
    await _register(client, email)
    burst = rate_limit.LIMITS["auth_login_email"].burst

    statuses = []
    for _ in range(burst + 1):
        r = await client.post("/api/v1/auth/login", json={"email": email, "password": "nope-1234"})
        statuses.append(r.status_code)
    assert statuses[:burst] == [401] * burst
    assert statuses[burst] == 429
    assert "wait" in r.json()["message"].lower()
    assert r.headers.get("retry-after") is not None

    # The right password does not get through while the throttle holds — that is the
    # lockout — and a DIFFERENT account on the same address is untouched below the
    # per-address ceiling.
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    assert r.status_code == 429
    other = _email()
    await _register(client, other)
    r = await client.post("/api/v1/auth/login", json={"email": other, "password": "pw-123456"})
    assert r.status_code == 200, r.text


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_email_throttle_is_case_and_whitespace_insensitive(client):
    email = _email()
    await _register(client, email)
    burst = rate_limit.LIMITS["auth_login_email"].burst
    for i in range(burst):
        spelled = email.upper() if i % 2 else f"  {email} "
        r = await client.post(
            "/api/v1/auth/login", json={"email": spelled, "password": "nope-1234"}
        )
        assert r.status_code in (401, 422)
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": "nope-1234"})
    assert r.status_code == 429


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_reset_requests_are_throttled_without_revealing_whether_the_address_exists(
    client,
):
    burst = rate_limit.LIMITS["auth_forgot_email"].burst
    unknown = _email()  # never registered
    statuses = []
    for _ in range(burst + 1):
        r = await client.post("/api/v1/auth/forgot-password", json={"email": unknown})
        statuses.append(r.status_code)
    assert statuses == [200] * burst + [429]

    known = _email()
    await _register(client, known)
    statuses = []
    for _ in range(burst + 1):
        r = await client.post("/api/v1/auth/forgot-password", json={"email": known})
        statuses.append(r.status_code)
    assert statuses == [200] * burst + [429]  # identical shape either way


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_per_address_ceiling_bounds_a_many_account_spray(client):
    burst = rate_limit.LIMITS["auth_login_ip"].burst
    statuses = []
    for _ in range(burst + 1):
        r = await client.post(
            "/api/v1/auth/login", json={"email": _email(), "password": "nope-1234"}
        )
        statuses.append(r.status_code)
    assert statuses[:burst] == [401] * burst
    assert statuses[burst] == 429
