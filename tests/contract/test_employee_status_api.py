"""Round 6 WP-F: GET /admin/luciel/status, PUT /admin/luciel/daily-brief and
PUT /admin/luciel/business-name — the employee layer's control-plane contract."""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


async def _login(client, *, with_luciel: bool = True) -> None:
    from app.auth import access

    email = f"emp-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup",
        json={"email": email, "password": "pw-123456", "captchaToken": "t"},
    )
    _acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    if with_luciel:
        await client.post(
            "/api/v1/admin/luciel",
            json={
                "name": "Aurora",
                "websiteUrl": "https://acme.example",
                "businessOneLiner": "Patios",
            },
        )


async def test_status_is_null_without_a_luciel_and_derived_with_one(client):
    await _login(client, with_luciel=False)
    r = await client.get("/api/v1/admin/luciel/status")
    assert r.status_code == 200 and r.json() is None

    await client.post(
        "/api/v1/admin/luciel",
        json={"name": "Aurora", "websiteUrl": "https://acme.example", "businessOneLiner": "Patios"},
    )
    r = await client.get("/api/v1/admin/luciel/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assistantName"] == "Aurora"
    assert body["businessShortName"] is None
    assert body["onDuty"] == "around the clock"
    assert body["conversationsUsed"] == 0
    assert body["freeAllowance"] == 50 and body["freeRemaining"] == 50
    assert body["teamReachableNow"] is None
    assert body["dailyBriefEnabled"] is True
    states = {c["id"]: c["state"] for c in body["capabilities"]}
    assert states["widget"] == "ready"
    assert states["knowledge"] == "attention"  # nothing uploaded yet
    assert states["escalation"] == "attention"  # no contact yet
    codes = [n["code"] for n in body["needs"]]
    assert codes[0] in ("no_escalation_contact", "knowledge_empty")
    assert "team_availability_unset" in codes and "allowed_origins_unset" in codes
    assert body["yesterday"]["conversations"] == 0 and body["today"]["conversations"] == 0
    assert set(body["today"]) >= {"date", "conversations", "byChannel", "leads", "escalations"}


async def test_daily_brief_and_business_name_round_trip(client):
    await _login(client)
    r = await client.put("/api/v1/admin/luciel/daily-brief", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["dailyBriefEnabled"] is False
    assert (await client.get("/api/v1/admin/luciel/status")).json()["dailyBriefEnabled"] is False

    r = await client.put(
        "/api/v1/admin/luciel/business-name", json={"businessShortName": "  Acme Patios "}
    )
    assert r.status_code == 200, r.text
    assert r.json()["businessShortName"] == "Acme Patios"
    assert (await client.get("/api/v1/admin/luciel/status")).json()[
        "businessShortName"
    ] == "Acme Patios"
    cleared = await client.put("/api/v1/admin/luciel/business-name", json={"businessShortName": ""})
    assert cleared.json()["businessShortName"] is None


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/api/v1/admin/luciel/business-name", {"businessShortName": "x" * 81}),
        ("/api/v1/admin/luciel/business-name", {"lucielName": "Aurora"}),
        ("/api/v1/admin/luciel/daily-brief", {"enabled": True, "hour": 6}),
        ("/api/v1/admin/luciel/daily-brief", {}),
    ],
)
async def test_refusals(client, path, body):
    await _login(client)
    r = await client.put(path, json=body)
    assert r.status_code == 422, r.text


async def test_the_business_name_reaches_the_widget_greeting(client):
    """The AI-identity disclosure says "for Acme" instead of "for this business" once
    the owner names the business — and never the Luciel's own name."""
    await _login(client)
    embed_key = (await client.get("/api/v1/admin/luciel")).json()["embedKeyPublicId"]
    before = await client.post("/api/v1/chat-widget/bootstrap", json={"embedKey": embed_key})
    assert "for this business" in before.json()["openingMessage"]
    await client.put("/api/v1/admin/luciel/business-name", json={"businessShortName": "Acme"})
    after = await client.post("/api/v1/chat-widget/bootstrap", json={"embedKey": embed_key})
    assert after.status_code == 200, after.text
    opening = after.json()["openingMessage"]
    assert "for Acme" in opening
    assert "for Aurora" not in opening
