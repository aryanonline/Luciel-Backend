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
from app.notifications.email_dispatcher import DeliveryOutcome

pytestmark = [pytest.mark.contract]


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
    # notices[] is always present (Legal §A5 in-dashboard notice), empty when none pending.
    assert set(body.keys()) == {"account", "nextRoute", "notices"}
    assert body["notices"] == []
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


# ---------------------------------------------------------------------------
# Regression tests for Axis2-F01 (logout audit event)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_logout_emits_audit_event(client):
    """Axis2-F01: POST /auth/logout must emit a logout audit event (§3.7.1a)."""
    from app.audit.log import AdminAuditLog
    from app.db.session import tenant_session
    from sqlalchemy import select

    # Sign up, verify, and log in.
    email = f"logout-test-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "t"},
    )
    from app.auth import access

    acct, token = await access.signup(email, "pw-123456")
    admin_id = acct.id
    r_verify = await client.post("/api/v1/auth/verify-email", json={"token": token})
    assert r_verify.status_code == 200

    # Logout
    r_logout = await client.post("/api/v1/auth/logout")
    assert r_logout.status_code == 204

    # Check that a logout audit event was emitted.
    async with tenant_session(admin_id) as s:
        evt = (
            await s.execute(
                select(AdminAuditLog).where(
                    AdminAuditLog.admin_id == admin_id,
                    AdminAuditLog.event_type == "logout",
                )
            )
        ).scalar_one_or_none()
    assert evt is not None, "logout must emit a 'logout' audit event (§3.7.1a)"


# ---------------------------------------------------------------------------
# Regression tests for Axis2-F02 (7-day unverified account reaper)
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_reaper_deletes_aged_unverified_account():
    """Axis2-F02: reap_unverified_accounts deletes aged unverified accounts."""
    from datetime import UTC, datetime

    from app.auth.reaper import reap_unverified_accounts
    from app.db.session import tenant_session
    from app.models.instance import Account, AccountState
    from sqlalchemy import select

    admin = uuid.uuid4()
    # Create an unverified account that's 8 days old.
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"stale-{admin}@reap.test",
                email_lower=f"stale-{admin}@reap.test",
                password_hash="x",
                state=AccountState.unverified,
                email_verified=False,
                created_at=datetime(2026, 6, 1, tzinfo=UTC),  # well past 7 days
            )
        )
        await s.flush()

    now = datetime(2026, 6, 20, tzinfo=UTC)
    async with tenant_session(admin) as s:
        reaped = await reap_unverified_accounts(s, now=now)

    assert admin in reaped, "Aged unverified account must be reaped"

    # Account row must be gone.
    async with tenant_session(admin) as s:
        acct = (await s.execute(select(Account).where(Account.id == admin))).scalar_one_or_none()
    assert acct is None, "Reaped account row must be deleted"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_reaper_spares_fresh_and_verified_accounts():
    """Axis2-F02: reaper must NOT delete fresh or verified accounts."""
    from datetime import UTC, datetime

    from app.auth.reaper import reap_unverified_accounts
    from app.db.session import tenant_session
    from app.models.instance import Account, AccountState

    fresh = uuid.uuid4()
    verified = uuid.uuid4()
    now = datetime(2026, 6, 20, tzinfo=UTC)

    async with tenant_session(fresh) as s:
        # Fresh unverified (only 1 day old — inside window)
        s.add(
            Account(
                id=fresh,
                admin_id=fresh,
                user_id=fresh,
                email=f"fresh-{fresh}@reap.test",
                email_lower=f"fresh-{fresh}@reap.test",
                password_hash="x",
                state=AccountState.unverified,
                email_verified=False,
                created_at=datetime(2026, 6, 19, tzinfo=UTC),  # 1 day ago — inside window
            )
        )
        await s.flush()

    async with tenant_session(verified) as s:
        # Verified account (even if old, must not be reaped)
        s.add(
            Account(
                id=verified,
                admin_id=verified,
                user_id=verified,
                email=f"verified-{verified}@reap.test",
                email_lower=f"verified-{verified}@reap.test",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
                created_at=datetime(2026, 6, 1, tzinfo=UTC),
            )
        )
        await s.flush()

    async with tenant_session(fresh) as s:
        reaped = await reap_unverified_accounts(s, now=now)

    assert fresh not in reaped, "Fresh unverified account (inside window) must NOT be reaped"
    assert verified not in reaped, "Verified account must NOT be reaped"


@pytest.mark.asyncio
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_signup_verify_link_targets_frontend_verify_route(client, monkeypatch):
    """Regression: the emailed verification link must point at the FRONTEND page
    (app_base_url + /verify?token=...), not the API host or /verify-email. A wrong
    host or path makes the customer's email link 404 (found in live dev testing)."""
    captured: dict[str, str] = {}

    async def _capture(session, *, admin_id, to, verify_url):
        captured["url"] = verify_url
        return DeliveryOutcome(status="sent")

    monkeypatch.setattr("app.api.v1.auth.email_dispatcher.send_verify_email", _capture)
    monkeypatch.setenv("LUCIEL_APP_BASE_URL", "https://vantagemind.ai")
    from app.config import get_settings

    get_settings.cache_clear()  # type: ignore[attr-defined]

    r = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": "linkcheck@example.com",
            "password": "LinkCheck!2026xy",
            "captchaToken": "x",
        },
    )
    assert r.status_code == 200
    assert "url" in captured, "verify email was not dispatched"
    url = captured["url"]
    assert url.startswith("https://vantagemind.ai/verify?token="), url
    assert "/verify-email?" not in url, f"link uses wrong path: {url}"
    get_settings.cache_clear()  # type: ignore[attr-defined]
