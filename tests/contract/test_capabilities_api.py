"""Contract test for the capability-grouping endpoint (Decision #8).

The product review's complaint was that scheduling looked like two unrelated switches
("check availability", "book appointment") when it is one thing an owner buys: Luciel
handles appointments. Read-before-write is a mechanic, not a choice the owner makes.

The grouping is SERVED rather than hardcoded in the UI for a specific reason: adding
reschedule/cancel to the capability was a backend catalog change, and a frontend that
hardcoded the member list would have silently kept rendering the old pair. So this test
pins the wire shape the dashboard renders from, and that the group names the single
connection it is gated on.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]


async def _signed_in(client) -> None:
    from app.auth import access

    email = f"cap-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "pw-123456", "captchaToken": "t"}
    )
    _a, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_scheduling_is_served_as_one_capability_over_the_calendar_connection(client):
    await _signed_in(client)
    r = await client.get("/api/v1/admin/luciel/capabilities")
    assert r.status_code == 200

    groups = {g["capability"]: g for g in r.json()}
    scheduling = groups["scheduling"]

    assert scheduling["label"] == "Appointment scheduling"
    assert scheduling["helpText"]
    # All four scheduling tools roll up into the ONE capability, in catalog order.
    assert scheduling["toolIds"] == [
        "check_availability",
        "book_appointment",
        "reschedule_appointment",
        "cancel_appointment",
    ]
    # And the group states the single connection the whole thing is gated on, so the UI
    # renders one Connect affordance and one "Reconnect needed" state for the capability.
    assert scheduling["connectionType"] == "calendar"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_every_served_group_agrees_with_the_tool_catalog(client):
    """The group is a projection of the catalog, so it cannot drift from it.

    A group that named a tool the catalog does not gate on that connection would send the
    dashboard's Connect button at the wrong connection."""
    from app.tools import catalog

    await _signed_in(client)
    served = (await client.get("/api/v1/admin/luciel/capabilities")).json()

    assert [g["capability"] for g in served] == [s.capability for s in catalog.CAPABILITIES]
    for group in served:
        for tool_id in group["toolIds"]:
            assert tool_id in catalog.ALL_TOOL_IDS, tool_id
            assert catalog.capability_of(tool_id) == group["capability"], tool_id
            assert catalog.TOOL_CONNECTION_TYPE[tool_id] == group["connectionType"], tool_id


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_capabilities_require_an_authenticated_admin(client):
    """The set of integrations we offer is not public, same rule as /providers."""
    r = await client.get("/api/v1/admin/luciel/capabilities")
    assert r.status_code == 401
