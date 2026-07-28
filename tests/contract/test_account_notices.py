"""In-dashboard account notices on the session payload (Legal §A5).

Reducing the free starter allowance owes every existing account 30 days' notice delivered
BOTH by email and "as an in-dashboard notification visible on next login". The dashboard
shell renders that second half off ``GET /api/v1/auth/me``, so the field not existing meant
the commitment could only ever be half-met. Asserted here: the field is always present on
the wire and empty by default, and it carries the pending reduction when one is announced.
The derivation rules live in tests/unit/test_account_notices.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from app.config import get_settings

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


async def _login(client) -> None:
    from app.auth import access

    email = f"notice-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "t"},
    )
    _acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})


@pytest.fixture
def announce(monkeypatch):
    """Announce a pending reduction the way the platform operator would (settings)."""

    def _announce(*, to: int, effective_at: datetime) -> None:
        settings = get_settings()
        monkeypatch.setattr(
            settings, "free_allowance_reduction_effective_at", effective_at.isoformat()
        )
        monkeypatch.setattr(settings, "free_allowance_reduction_to", to)

    return _announce


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_me_always_carries_notices_and_defaults_to_empty(client):
    await _login(client)
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 200
    assert r.json()["notices"] == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_pending_allowance_reduction_is_surfaced_with_its_effective_date(client, announce):
    await _login(client)
    effective = datetime.now(UTC) + timedelta(days=45)
    announce(to=25, effective_at=effective)

    r = await client.get("/api/v1/auth/me")
    body = r.json()["notices"]
    assert len(body) == 1
    notice = body[0]
    assert notice["kind"] == "allowance_reduction"
    assert notice["noticeId"]
    assert notice["title"]
    # The body must name the new number and the date, since this notice IS the notice.
    assert "25" in notice["body"]
    assert effective.date().isoformat() in notice["body"]
    assert notice["effectiveAt"].startswith(effective.date().isoformat())
