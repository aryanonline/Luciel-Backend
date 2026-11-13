"""Round 6 WP-E (F054): PUT /admin/luciel/team-availability — the optional description
of when the HUMAN team is reachable. Round-trips through GET, refuses what could never
work (a made-up timezone, a window without length, an after-hours text with no
number), and never touches Luciel's own availability."""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.contract]

PUT = "/api/v1/admin/luciel/team-availability"


async def _login_with_luciel(client) -> None:
    from app.auth import access

    email = f"hours-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "t"},
    )
    _acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    await client.post(
        "/api/v1/admin/luciel",
        json={"name": "Acme", "websiteUrl": "https://acme.example", "businessOneLiner": "Patios"},
    )


async def test_unset_by_default_then_round_trips(client):
    await _login_with_luciel(client)
    before = (await client.get("/api/v1/admin/luciel")).json()
    assert before["teamAvailability"] is None
    assert before["timezone"] is None

    body = {
        "timezone": "America/Toronto",
        "enabled": True,
        "weekly": [
            {"day": 0, "start": "09:00", "end": "17:00"},
            {"day": 4, "start": "22:00", "end": "06:00"},
        ],
        "closures": [{"date": "2026-12-25", "label": "Christmas"}],
        "afterHours": {
            "escalation": {
                "channel": "sms",
                "contactSms": "+1 (604) 555-9999",
                "ccOwnerEmail": True,
            }
        },
    }
    r = await client.put(PUT, json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["timezone"] == "America/Toronto"
    ta = out["teamAvailability"]
    assert ta["enabled"] is True
    assert ta["weekly"] == body["weekly"]
    assert ta["closures"] == [{"date": "2026-12-25", "label": "Christmas"}]
    # The number is stored in the one shape a text can be delivered to (F084).
    assert ta["afterHours"]["escalation"]["contactSms"] == "+16045559999"
    assert ta["afterHours"]["escalation"]["ccOwnerEmail"] is True
    again = (await client.get("/api/v1/admin/luciel")).json()
    assert again["teamAvailability"] == ta
    assert again["timezone"] == "America/Toronto"


async def test_turning_it_off_keeps_the_grid_and_the_timezone(client):
    """Off is a switch, not an erase: the owner's grid survives for next time and the
    timezone stays because the morning brief reads it on its own."""
    await _login_with_luciel(client)
    grid = [{"day": 1, "start": "08:00", "end": "12:00"}]
    await client.put(PUT, json={"timezone": "Europe/Berlin", "enabled": True, "weekly": grid})
    r = await client.put(PUT, json={"timezone": "Europe/Berlin", "enabled": False, "weekly": grid})
    assert r.status_code == 200, r.text
    assert r.json()["teamAvailability"]["enabled"] is False
    assert r.json()["teamAvailability"]["weekly"] == grid
    assert r.json()["timezone"] == "Europe/Berlin"


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ({"timezone": "Mars/Olympus_Mons", "enabled": True}, "timezone"),
        ({"enabled": True}, "timezone"),
        (
            {
                "timezone": "UTC",
                "enabled": True,
                "weekly": [{"day": 0, "start": "9am", "end": "17:00"}],
            },
            "24-hour time",
        ),
        (
            {
                "timezone": "UTC",
                "enabled": True,
                "weekly": [{"day": 0, "start": "09:00", "end": "09:00"}],
            },
            "end after it starts",
        ),
        (
            {
                "timezone": "UTC",
                "enabled": True,
                "weekly": [{"day": 7, "start": "09:00", "end": "17:00"}],
            },
            "day",
        ),
        (
            {"timezone": "UTC", "enabled": True, "closures": [{"date": "Christmas"}]},
            "2026-12-25",
        ),
        (
            {"timezone": "UTC", "enabled": True, "afterHours": {"escalation": {"channel": "sms"}}},
            "number",
        ),
        (
            {
                "timezone": "UTC",
                "enabled": True,
                "afterHours": {
                    "escalation": {"channel": "email", "contactEmail": "not-an-address"}
                },
            },
            "email",
        ),
        ({"timezone": "UTC", "enabled": True, "lucielHours": "09:00-17:00"}, "lucielHours"),
    ],
)
async def test_what_could_never_work_is_refused_with_the_fix(client, body, fragment):
    await _login_with_luciel(client)
    r = await client.put(PUT, json=body)
    assert r.status_code == 422, r.text
    assert fragment.lower() in r.text.lower()
    # Nothing was stored.
    assert (await client.get("/api/v1/admin/luciel")).json()["teamAvailability"] is None
