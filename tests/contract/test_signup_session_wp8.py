"""F148 (2026-09-05 audit): signup issues a session so the verify wall can resend.

* A fresh signup sets the HttpOnly session cookie; the wall's resend works at once.
* The session is gated: an admin route still refuses the unverified account.
* Re-signing up on a VERIFIED email issues no session (never a password-less login).
"""

from __future__ import annotations

import uuid

import pytest
from app.config import get_settings

pytestmark = [pytest.mark.asyncio]


def _email() -> str:
    return f"wp8-{uuid.uuid4().hex[:10]}@example.com"


async def _signup(client, email: str):
    return await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "tok"},
    )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_signup_sets_a_session_so_resend_works_on_the_verify_wall(client):
    cookie = get_settings().session_cookie_name
    r = await _signup(client, _email())
    assert r.status_code == 200
    assert cookie in r.cookies, "signup must issue the session the verify wall relies on"
    # The wall's action works immediately, with no login in between.
    resend = await client.post("/api/v1/auth/resend-verification")
    assert resend.status_code == 200, resend.text
    assert resend.json()["ok"] is True
    # …but the session is still gated everywhere that needs a verified account.
    gated = await client.get("/api/v1/admin/luciel")
    assert gated.status_code == 403


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_re_signup_on_a_verified_email_issues_no_session(client):
    from app.auth import access

    email = _email()
    first = await _signup(client, email)
    assert first.status_code == 200
    client.cookies.clear()
    # Verify out of band, then sign up again with a DIFFERENT password.
    _acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    client.cookies.clear()
    again = await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "attacker-pw-1", "captchaToken": "tok"},
    )
    assert again.status_code == 200  # non-enumerating shape
    assert get_settings().session_cookie_name not in again.cookies
